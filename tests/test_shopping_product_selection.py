"""Unit tests for set_shopping_product_selection."""
import sys
from pathlib import Path
import unittest
from unittest import mock

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401

from pydantic import ValidationError
from shield.models_shopping import SetShoppingProductSelectionRequest


class SetShoppingProductSelectionModelTests(unittest.TestCase):
    def test_valid_request(self):
        req = SetShoppingProductSelectionRequest(
            ad_group_id="2000000001",
            offers=["SKU-100-S", "SKU-100-L"],
            cpc_ceiling_eur=0.50,
            dry_run=True
        )
        self.assertEqual(req.ad_group_id, "2000000001")
        self.assertEqual(len(req.offers), 2)
        self.assertTrue(req.dry_run)

    def test_empty_offers_fails(self):
        with self.assertRaises(ValidationError):
            SetShoppingProductSelectionRequest(
                ad_group_id="2000000001",
                offers=[]
            )

    def test_invalid_ad_group_id_fails(self):
        with self.assertRaises(ValidationError):
            SetShoppingProductSelectionRequest(
                ad_group_id="invalid",
                offers=["SKU-100-S"]
            )


if __name__ == "__main__":
    unittest.main()
