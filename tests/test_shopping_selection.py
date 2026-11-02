"""
Resolving the offers of a Shopping selection against the catalog: case does not matter, Google Ads writes the ID, Merchant
Center says whether the product can show.

Pure: no network. The reads of both catalogs are covered where the tool uses them (test_shopping_campaign.py).

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_shopping_selection.py" -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401  (pins the fictional test policy before anything reads it)

from core.merchant_client import MerchantApiError
from tools import shopping_selection
from tools.shopping_selection import APPROVED, DISAPPROVED, PENDING, UNKNOWN, read_gmc_states, resolve_offers, shopping_ads_state

ADS = ["sku-003-clearance", "sku-003-L", "sku-001-clearance"]
GMC = {"SKU-003-clearance": APPROVED, "SKU-003-L": PENDING, "SKU-001-clearance": DISAPPROVED, "SKU-002-clearance": APPROVED}


def product(**countries):
    return {"offerId": "SKU-003-clearance", "productStatus": {"destinationStatuses": [
        {"reportingContext": "FREE_LISTINGS", "disapprovedCountries": ["DE"]}, {"reportingContext": "SHOPPING_ADS", **countries}]}}


class ResolveTests(unittest.TestCase):
    def test_case_is_ignored_and_the_ids_come_back_as_google_ads_spells_them(self):
        result = resolve_offers(["SKU-003-CLEARANCE", "SKU-003-L"], ADS, None)
        self.assertEqual(result.item_ids, ["sku-003-clearance", "sku-003-L"])
        self.assertEqual((result.unknown, result.not_eligible, result.pending, result.source), ([], [], [], "google_ads"))

    def test_the_order_of_the_request_is_kept(self):
        self.assertEqual(resolve_offers(["SKU-003-L", "SKU-003-clearance"], ADS, None).item_ids, ["sku-003-L", "sku-003-clearance"])

    def test_an_unknown_offer_is_reported_as_asked(self):
        result = resolve_offers(["SKU-003-clearance", "SKU-999-clearance"], ADS, None)
        self.assertEqual(result.unknown, ["SKU-999-clearance"])
        self.assertEqual(result.item_ids, ["sku-003-clearance"])

    def test_merchant_center_decides_whether_a_product_can_show(self):
        result = resolve_offers(["SKU-003-clearance", "SKU-003-L", "SKU-001-clearance"], ADS, GMC)
        self.assertEqual(result.not_eligible, ["SKU-001-clearance"])
        self.assertEqual(result.pending, ["SKU-003-L"])
        self.assertTrue(result.approval_checked)
        self.assertEqual(result.source, "google_ads+merchant_center")

    def test_without_merchant_center_nothing_is_said_about_approval(self):
        result = resolve_offers(["SKU-001-clearance"], ADS, None)
        self.assertEqual((result.not_eligible, result.pending, result.approval_checked), ([], [], False))

    def test_merchant_center_fills_in_what_google_ads_does_not_list_yet(self):
        result = resolve_offers(["SKU-003-clearance", "SKU-002-clearance"], ADS, GMC)
        self.assertEqual(result.item_ids, ["sku-003-clearance", "SKU-002-clearance"])   # Google Ads spelling where it has one, the catalog's otherwise
        self.assertEqual(result.unknown, [])

    def test_when_google_ads_lists_nothing_the_catalog_decides(self):
        result = resolve_offers(["SKU-002-clearance", "SKU-404"], [], GMC)
        self.assertEqual((result.item_ids, result.unknown, result.source), (["SKU-002-clearance"], ["SKU-404"], "merchant_center"))

    def test_nothing_to_check_against_is_unreadable_not_unknown(self):
        result = resolve_offers(["SKU-003-clearance"], [], None)
        self.assertTrue(result.unreadable)
        self.assertEqual((result.item_ids, result.unknown), ([], []))
        self.assertFalse(resolve_offers(["SKU-003-clearance"], ADS, None).unreadable)


class ApprovalTests(unittest.TestCase):
    def test_only_the_shopping_ads_context_counts(self):
        # the product is disapproved for free listings in this fixture; Shopping ads is what the campaign needs
        self.assertEqual(shopping_ads_state(product(approvedCountries=["DE", "AT"]), ["DE"]), APPROVED)

    def test_all_countries_of_the_campaign_must_be_approved(self):
        self.assertEqual(shopping_ads_state(product(approvedCountries=["DE"]), ["DE", "AT"]), UNKNOWN)
        self.assertEqual(shopping_ads_state(product(approvedCountries=["DE"], pendingCountries=["AT"]), ["DE", "AT"]), PENDING)
        self.assertEqual(shopping_ads_state(product(approvedCountries=["DE"], disapprovedCountries=["AT"]), ["DE", "AT"]), DISAPPROVED)

    def test_a_disapproval_in_another_country_does_not_count(self):
        self.assertEqual(shopping_ads_state(product(approvedCountries=["DE"], disapprovedCountries=["FR"]), ["DE"]), APPROVED)

    def test_no_shopping_ads_status_or_no_country_is_unknown(self):
        self.assertEqual(shopping_ads_state({"offerId": "x"}, ["DE"]), UNKNOWN)
        self.assertEqual(shopping_ads_state(product(approvedCountries=["DE"]), []), UNKNOWN)


class CatalogReadTests(unittest.TestCase):
    def test_the_merchant_center_catalog_is_each_offer_with_its_state(self):
        req = mock.Mock(merchant_id="1234567890", geo_target_constants=[2276])
        pages = mock.MagicMock(return_value=[product(approvedCountries=["DE"]), {"title": "no id"}])
        with mock.patch.object(shopping_selection, "merchant_pages", pages):
            self.assertEqual(read_gmc_states(req), {"SKU-003-clearance": APPROVED})
        self.assertEqual(pages.call_args.args[:3], ("products/v1/accounts/1234567890/products", "products", "products.list"))

    def test_an_unreachable_merchant_center_gives_none_not_an_error(self):
        with mock.patch.object(shopping_selection, "merchant_pages", side_effect=MerchantApiError("IMPERSONATION_DENIED", "no right")):
            self.assertIsNone(read_gmc_states(mock.Mock(merchant_id="1234567890", geo_target_constants=[2276])))


if __name__ == "__main__":
    unittest.main()
