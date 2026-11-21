"""
set_shopping_product_selection builds a CreateShoppingCampaignRequest only to reuse the offer check; it creates no campaign.
Its placeholder name used to be written in the code ("<advertiser> Shopping Dummy"), which the leak scan of the public export
refuses. The name now comes from the policy: it must satisfy the request model under any prefix, and the source must carry no
advertiser word.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_shopping_placeholder_name.py" -v
"""
import sys
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
try:  # pins the fictional test policy; absent in a clean checkout
    import policy_env  # noqa: F401
except ImportError:
    pass

from core import policy
from shield.models_shopping import CreateShoppingCampaignRequest
from tools import shopping_tools


def request_with(name):
    return CreateShoppingCampaignRequest(name=name, merchant_id="123456789", feed_label="FEED", offers=["SKU-001"])


class TestPlaceholderName(unittest.TestCase):
    def test_it_starts_with_the_policy_prefix_and_passes_the_request_model(self):
        for prefix in ("ExampleShop", "Acme Store", "X"):
            with policy.override({"shopping": {"name_prefix": prefix}}):
                name = shopping_tools._placeholder_campaign_name()
                self.assertTrue(name.startswith(prefix), name)
                self.assertEqual(request_with(name).name, name)

    def test_with_no_prefix_in_the_policy_it_is_still_a_valid_name(self):
        with policy.override({"shopping": {"name_prefix": ""}}):
            name = shopping_tools._placeholder_campaign_name()
            self.assertEqual(name, "selection check")
            self.assertEqual(request_with(name).name, name)

    def test_the_source_of_the_tool_carries_no_advertiser_word(self):
        source = (PKG / "tools" / "shopping_tools.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("shopping dummy", source)


if __name__ == "__main__":
    unittest.main()
