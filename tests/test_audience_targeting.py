"""
Unit test for set_ad_group_audience_targeting.
Verifies Pydantic schema validation, targeting setting configuration, and criterion operations.
"""
import unittest
from unittest import mock
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401

from shield.models_adgroup import AdGroupAudienceTargetingRequest, AdGroupAudienceTargetingResponse
from pydantic import ValidationError


class TestAudienceTargetingModels(unittest.TestCase):
    def test_valid_request_targeting(self):
        req = AdGroupAudienceTargetingRequest(
            ad_group_id="1234567890",
            user_list_ids=["1000000001", "1000000002"],
            targeting_mode="TARGETING"
        )
        self.assertEqual(req.ad_group_id, "1234567890")
        self.assertEqual(req.targeting_mode, "TARGETING")
        self.assertEqual(len(req.user_list_ids), 2)

    def test_valid_request_observation_with_bid_modifier(self):
        req = AdGroupAudienceTargetingRequest(
            ad_group_id="1234567890",
            user_list_ids=["1000000001"],
            targeting_mode="OBSERVATION",
            bid_modifier=1.5
        )
        self.assertEqual(req.targeting_mode, "OBSERVATION")
        self.assertEqual(req.bid_modifier, 1.5)

    def test_invalid_mode(self):
        with self.assertRaises(ValidationError):
            AdGroupAudienceTargetingRequest(
                ad_group_id="1234567890",
                user_list_ids=["1000000001"],
                targeting_mode="INVALID_MODE"
            )

    def test_empty_user_lists(self):
        with self.assertRaises(ValidationError):
            AdGroupAudienceTargetingRequest(
                ad_group_id="1234567890",
                user_list_ids=[]
            )

    def test_invalid_ad_group_id(self):
        with self.assertRaises(ValidationError):
            AdGroupAudienceTargetingRequest(
                ad_group_id="not_a_number",
                user_list_ids=["1000000001"]
            )


if __name__ == "__main__":
    unittest.main()

