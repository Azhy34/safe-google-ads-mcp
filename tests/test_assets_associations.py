"""
get_assets_associations: the 'Associations' tab for one campaign or one ad group, not for the whole account.

31.10.2026 the tool answered for the whole account (150 rows, ~70 000 characters, more than one tool answer holds), so the
images of a single group had to be picked out of a saved file by a script. Now campaign_id / ad_group_id narrow the query,
and an image record carries its pixel size. The Google Ads service is faked: it returns only the rows the WHERE of the query
asks for and refuses a filter on a field that is not selected, as the live API does.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_assets_associations.py" -v
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401  (pins the fictional test policy before anything reads it)

from shield import cloud_sink, error_shield
from shield.models_asset import AssetAssociationsRequest
from tools.asset_tools import get_assets_associations

CAMPAIGN_A, CAMPAIGN_B = "111", "222"
GROUP_1, GROUP_2, GROUP_3 = "1001", "1002", "2001"


def asset(asset_id, name, text="", link="", width=None, height=None):
    image = SimpleNamespace(full_size=SimpleNamespace(url=f"https://cdn.example/{asset_id}.jpg", width_pixels=width, height_pixels=height)) \
        if width else SimpleNamespace(full_size=None)
    return SimpleNamespace(
        id=asset_id, name=name, text_asset=SimpleNamespace(text=text), sitelink_asset=SimpleNamespace(link_text=link),
        callout_asset=SimpleNamespace(callout_text=""), structured_snippet_asset=SimpleNamespace(header=""), image_asset=image)


def enum(name):
    return SimpleNamespace(name=name)


class FakeGoogleAds:
    """An account with one account-level asset, two campaigns and three ad groups; search() honours the WHERE of the query."""

    def __init__(self):
        self.queries = []
        self.customer_rows = [dict(asset=asset(1, "ExampleShop", text="ExampleShop"), field="BUSINESS_NAME")]
        self.campaign_rows = [
            dict(campaign=(CAMPAIGN_A, "Site"), asset=asset(11, "sl1", link="Wald"), field="SITELINK"),
            dict(campaign=(CAMPAIGN_A, "Site"), asset=asset(12, "sl2", link="Meer"), field="SITELINK"),
            dict(campaign=(CAMPAIGN_B, "Other"), asset=asset(13, "sl3", link="Muster"), field="SITELINK"),
        ]
        self.group_rows = [
            dict(campaign=CAMPAIGN_A, group=(GROUP_1, "Group One"), asset=asset(21, "wide", width=1200, height=628), field="AD_IMAGE"),
            dict(campaign=CAMPAIGN_A, group=(GROUP_1, "Group One"), asset=asset(22, "square", width=1024, height=1024), field="AD_IMAGE"),
            dict(campaign=CAMPAIGN_A, group=(GROUP_2, "Group Two"), asset=asset(23, "other", width=1200, height=628), field="AD_IMAGE"),
            dict(campaign=CAMPAIGN_B, group=(GROUP_3, "Group Three"), asset=asset(24, "foreign", width=300, height=300), field="AD_IMAGE"),
            dict(campaign=CAMPAIGN_A, group=(GROUP_1, "Group One"), asset=asset(25, "group sitelink", link="Dschungel"), field="SITELINK"),
        ]
        self.ga_service = mock.MagicMock()
        self.ga_service.search.side_effect = self._search
        self.client = mock.MagicMock()
        self.client.get_service.return_value = self.ga_service

    @staticmethod
    def _match(query, key, value):
        found = re.search(rf"{re.escape(key)} = '?(\w+)'?", query)
        return found is None or found.group(1) == value

    def _search(self, customer_id, query):
        self.queries.append(query)
        head = query.split("FROM")[0]
        # The live API refuses WHERE on campaign.id / ad_group.id unless the field is selected too
        # (query_error.EXPECTED_REFERENCED_FIELD_IN_SELECT_CLAUSE, hit on 31.10.2026 enabling a real group)
        for field in ("campaign.id", "ad_group.id"):
            if f"WHERE" in query and re.search(rf"AND {re.escape(field)} =", query) and field not in head:
                raise AssertionError(f"live API would refuse: {field} must be in SELECT: {query.strip()[:80]}")
        status = SimpleNamespace(name="ENABLED")
        if "FROM customer_asset" in query:
            return [SimpleNamespace(asset=r["asset"], customer_asset=SimpleNamespace(field_type=enum(r["field"]), status=status))
                    for r in self.customer_rows if self._match(query, "customer_asset.field_type", r["field"])]
        if "FROM campaign_asset" in query:
            return [SimpleNamespace(campaign=SimpleNamespace(id=int(r["campaign"][0]), name=r["campaign"][1]), asset=r["asset"],
                                    campaign_asset=SimpleNamespace(field_type=enum(r["field"]), status=status))
                    for r in self.campaign_rows
                    if self._match(query, "campaign.id", r["campaign"][0]) and self._match(query, "campaign_asset.field_type", r["field"])]
        if "FROM ad_group_asset" in query:
            return [SimpleNamespace(campaign=SimpleNamespace(id=int(r["campaign"])),
                                    ad_group=SimpleNamespace(id=int(r["group"][0]), name=r["group"][1]), asset=r["asset"],
                                    ad_group_asset=SimpleNamespace(field_type=enum(r["field"]), status=status))
                    for r in self.group_rows
                    if self._match(query, "campaign.id", r["campaign"]) and self._match(query, "ad_group.id", r["group"][0])
                    and self._match(query, "ad_group_asset.field_type", r["field"])]
        raise AssertionError(f"unexpected query: {query}")


class AssetAssociationsTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", Path(tmp.name) / "audit.jsonl"),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, fake, **filters):
        with mock.patch("tools.asset_tools.get_ads_client", return_value=fake.client):
            return get_assets_associations(customer_id="1", **filters)

    def test_without_filters_all_three_levels_are_read_as_before(self):
        fake = FakeGoogleAds()
        result = self.run_tool(fake)
        self.assertTrue(result.success)
        self.assertEqual(len(fake.queries), 3)
        self.assertFalse(any("campaign.id =" in q or "ad_group.id =" in q for q in fake.queries))
        self.assertEqual({r.level for r in result.data}, {"CUSTOMER", "CAMPAIGN", "AD_GROUP"})
        self.assertEqual(len(result.data), 1 + 3 + 5)

    def test_an_ad_group_filter_reads_only_that_group_and_names_the_image_size(self):
        fake = FakeGoogleAds()
        result = self.run_tool(fake, level="AD_GROUP", ad_group_id=GROUP_1, field_type="AD_IMAGE")
        self.assertEqual(len(fake.queries), 1)
        self.assertIn("FROM ad_group_asset", fake.queries[0])
        self.assertIn(f"AND ad_group.id = {GROUP_1}", fake.queries[0])
        self.assertEqual([r.asset_id for r in result.data], ["21", "22"])
        self.assertEqual([r.asset_size for r in result.data], ["1200x628 (1.91:1)", "1024x1024 (1.00:1)"])
        self.assertEqual({r.entity_id for r in result.data}, {GROUP_1})
        self.assertEqual({r.field_type for r in result.data}, {"AD_IMAGE"})

    def test_an_ad_group_filter_alone_skips_account_and_campaign_levels(self):
        fake = FakeGoogleAds()
        result = self.run_tool(fake, ad_group_id=GROUP_1)
        self.assertEqual(len(fake.queries), 1)
        self.assertEqual({r.level for r in result.data}, {"AD_GROUP"})
        self.assertEqual({r.asset_id for r in result.data}, {"21", "22", "25"})

    def test_a_campaign_filter_reads_the_campaign_and_its_groups_but_not_the_account(self):
        fake = FakeGoogleAds()
        result = self.run_tool(fake, campaign_id=CAMPAIGN_A)
        self.assertEqual(len(fake.queries), 2)
        self.assertFalse(any("FROM customer_asset" in q for q in fake.queries))
        self.assertTrue(all(f"AND campaign.id = {CAMPAIGN_A}" in q for q in fake.queries))
        self.assertEqual({r.asset_id for r in result.data}, {"11", "12", "21", "22", "23", "25"})
        self.assertNotIn("CUSTOMER", {r.level for r in result.data})

    def test_a_campaign_filter_with_a_field_type_finds_its_sitelinks(self):
        fake = FakeGoogleAds()
        result = self.run_tool(fake, campaign_id=CAMPAIGN_A, field_type="SITELINK", level="CAMPAIGN")
        self.assertEqual(len(fake.queries), 1)
        self.assertEqual([r.asset_text for r in result.data], ["Wald", "Meer"])
        self.assertTrue(all(r.asset_size is None for r in result.data))          # a text asset has no pixel size

    def test_level_and_field_type_are_upper_cased(self):
        fake = FakeGoogleAds()
        result = self.run_tool(fake, level="ad_group", field_type="ad_image", ad_group_id=GROUP_2)
        self.assertIn("ad_group_asset.field_type = 'AD_IMAGE'", fake.queries[0])
        self.assertEqual([r.asset_id for r in result.data], ["23"])

    def test_the_filters_are_checked_before_they_reach_a_query(self):
        for bad in (dict(field_type="AD_IMAGE' OR 1=1"), dict(field_type="x"), dict(campaign_id="2000000001 OR"),
                    dict(campaign_id="Site"), dict(ad_group_id="abc"), dict(ad_group_id="1;2"), dict(level="ACCOUNT"),
                    dict(level="CAMPAIGN", ad_group_id=GROUP_1), dict(level="CUSTOMER", ad_group_id=GROUP_1),
                    dict(level="CUSTOMER", campaign_id=CAMPAIGN_A)):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                AssetAssociationsRequest(**bad)

    def test_the_tool_refuses_a_bad_filter_without_asking_google(self):
        fake = FakeGoogleAds()
        result = self.run_tool(fake, ad_group_id="abc")
        self.assertFalse(result.success)
        self.assertEqual(fake.queries, [])


class ServerRegistrationTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", Path(tmp.name) / "audit.jsonl"),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_server_tool_takes_the_two_new_filters_and_rejects_a_bad_id(self):
        import server
        tool = server.mcp._tool_manager._tools["get_assets_associations"]
        self.assertTrue({"level", "field_type", "campaign_id", "ad_group_id"} <= set(tool.parameters["properties"]))
        with mock.patch("tools.asset_tools.get_ads_client", side_effect=AssertionError("Google must not be asked")):
            answer = json.loads(server.get_assets_associations(ad_group_id="abc"))
        self.assertFalse(answer["success"])
        self.assertEqual(answer["error"]["code"], "REQUEST_REJECTED")

    def test_the_registered_tool_schema_lists_the_filters(self):
        """The schema a client builds from the registry (what skills/sync_schemas.py writes for other clients) has the filters."""
        import server
        parameters = server.mcp._tool_manager._tools["get_assets_associations"].parameters
        self.assertTrue({"level", "field_type", "campaign_id", "ad_group_id"} <= set(parameters["properties"]))


if __name__ == "__main__":
    unittest.main()
