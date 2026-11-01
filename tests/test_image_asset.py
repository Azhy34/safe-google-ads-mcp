"""
create_image_asset: an image of example-shop.test becomes an image asset of the ad account.

No network and no account: the download is faked, the Google Ads services are mocked (the operation and the enums are
the real types), the pictures are drawn with Pillow. The audit trail goes to a temporary file.

Run:
  <venv>/python.exe -m unittest discover -s google-ads/tests -p "test_image_asset.py" -v
"""
import hashlib
import importlib
import io
import os
import pkgutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
from pydantic import ValidationError

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "tests"))
import policy_env  # noqa: F401  (pins the fictional test policy before anything reads it)

import google.ads.googleads as googleads
from shield import cloud_sink, error_shield
from shield.error_shield import MUTATION_FUNCTIONS, build_request
from shield.models_image import CreateImageAssetRequest, check_site_image_url
from tools import image_tools
from tools.eval_tools import LEVER_GROUPS
from tools.image_tools import ImageSourceError, create_image_asset, fetch_site_image, prepare_image

VERSION = sorted(m.name for m in pkgutil.iter_modules(googleads.__path__) if m.name.startswith("v"))[-1]
AssetOperation = importlib.import_module(f"google.ads.googleads.{VERSION}.services.types.asset_service").AssetOperation
AssetType = importlib.import_module(f"google.ads.googleads.{VERSION}.enums.types.asset_type").AssetTypeEnum.AssetType
MimeType = importlib.import_module(f"google.ads.googleads.{VERSION}.enums.types.mime_type").MimeTypeEnum.MimeType

OG = "https://example-shop.test/img/og-blumenwiese.jpg"


def picture(width, height, fmt="JPEG", mode="RGB"):
    """Bytes of a plain picture with a gradient, so a JPEG is not trivially tiny."""
    image = Image.new(mode, (width, height))
    pixels = image.load()
    for x in range(0, width, 7):
        for y in range(0, height, 7):
            value = (x * 255 // max(width, 1), y * 255 // max(height, 1), 120)
            pixels[x, y] = value if mode == "RGB" else value + (255,)
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self.headers, self._body, self.closed = status, headers or {}, body, False

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self):
        self.closed = True


class ModelTests(unittest.TestCase):
    def test_only_https_pages_of_our_own_site_pass(self):
        for good in (OG, "https://www.example-shop.test/img/x.webp", "https://example-shop.test:443/img/x.jpg"):
            CreateImageAssetRequest(source_url=good, ratio="LANDSCAPE")
        for bad in ("http://example-shop.test/img/x.jpg",              # not https
                    "https://example.com/img/x.jpg",                    # foreign host
                    "https://example-shop.test.evil.com/img/x.jpg",    # look-alike host
                    "https://user:pw@example-shop.test/img/x.jpg",     # credentials
                    "https://example-shop.test:8443/img/x.jpg",        # other port
                    "https://example-shop.test/",                      # no file
                    "ftp://example-shop.test/img/x.jpg",
                    ""):
            with self.assertRaises(ValidationError, msg=bad):
                CreateImageAssetRequest(source_url=bad, ratio="LANDSCAPE")

    def test_the_ratio_and_the_name_are_checked(self):
        with self.assertRaises(ValidationError):
            CreateImageAssetRequest(source_url=OG, ratio="PORTRAIT")
        with self.assertRaises(ValidationError):
            CreateImageAssetRequest(source_url=OG, ratio="SQUARE", name="x'; DROP")
        self.assertEqual(CreateImageAssetRequest(source_url=OG, ratio="SQUARE", name="  Blumen 1x1  ").name, "Blumen 1x1")

    def test_a_rejected_request_is_logged_as_request_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(error_shield, "AUDIT_FILE", Path(tmp) / "audit.jsonl"), \
                mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}):
            req, rejected = build_request("create_image_asset", CreateImageAssetRequest,
                                          source_url="https://example.com/a.jpg", ratio="LANDSCAPE", name=None, dry_run=False)
        self.assertIsNone(req)
        self.assertIn("REQUEST_REJECTED", rejected)

    def test_the_tool_is_a_mutation_and_an_assets_lever_and_is_registered(self):
        self.assertIn("create_image_asset", MUTATION_FUNCTIONS)
        self.assertIn("create_image_asset", LEVER_GROUPS["assets"])
        source = (PKG / "server.py").read_text(encoding="utf-8")
        self.assertIn("def create_image_asset(source_url: str", source)
        self.assertIn('build_request("create_image_asset", CreateImageAssetRequest', source)


class PrepareImageTests(unittest.TestCase):
    def test_the_site_og_size_goes_up_as_it_is(self):
        data = picture(1200, 630)
        result = prepare_image(data, "LANDSCAPE")
        self.assertEqual((result["width"], result["height"]), (1200, 630))
        self.assertEqual(result["bytes"], data)          # inside the 1 % tolerance: no cut, no re-encoding
        self.assertEqual(result["mime"], "IMAGE_JPEG")
        self.assertFalse(result["cropped"])

    def test_webp_of_another_ratio_is_cut_shrunk_and_sent_as_jpeg(self):
        result = prepare_image(picture(1448, 965, "WEBP"), "LANDSCAPE")
        self.assertTrue(result["cropped"])
        self.assertTrue(result["resized"])
        self.assertEqual(result["mime"], "IMAGE_JPEG")
        self.assertLessEqual(result["width"], 1200)
        self.assertAlmostEqual(result["width"] / result["height"], 1.91, delta=1.91 * 0.01)
        self.assertEqual(Image.open(io.BytesIO(result["bytes"])).format, "JPEG")

    def test_a_square_png_stays_a_png(self):
        data = picture(1000, 1000, "PNG")
        result = prepare_image(data, "SQUARE")
        self.assertEqual(result["bytes"], data)
        self.assertEqual(result["mime"], "IMAGE_PNG")

    def test_a_wide_picture_is_cut_to_a_square_from_the_centre(self):
        result = prepare_image(picture(2000, 1000), "SQUARE")
        self.assertEqual((result["width"], result["height"]), (1000, 1000))
        self.assertTrue(result["cropped"])

    def test_a_picture_below_the_google_minimum_is_refused(self):
        with self.assertRaises(ImageSourceError):
            prepare_image(picture(500, 262), "LANDSCAPE")   # 1.91:1 but under 600x314
        with self.assertRaises(ImageSourceError):
            prepare_image(picture(250, 250), "SQUARE")      # under 300x300

    def test_a_file_that_is_not_an_image_is_refused(self):
        with self.assertRaises(ImageSourceError):
            prepare_image(b"<html>404</html>", "LANDSCAPE")

    def test_a_cut_that_leaves_too_little_is_refused(self):
        # 400x400 is a fine square but cut to 1.91:1 it is 400x209: under the 600x314 minimum
        with self.assertRaises(ImageSourceError):
            prepare_image(picture(400, 400), "LANDSCAPE")
        # 900x900 cut to 1.91:1 is 900x471: enough
        self.assertEqual(prepare_image(picture(900, 900), "LANDSCAPE")["height"], 471)


class FetchTests(unittest.TestCase):
    def test_a_redirect_inside_the_site_is_followed(self):
        first = FakeResponse(301, headers={"Location": "/img/new.jpg"})
        second = FakeResponse(200, body=b"abc")
        with mock.patch("requests.get", side_effect=[first, second]) as get:
            self.assertEqual(fetch_site_image(OG), b"abc")
        self.assertEqual(get.call_args_list[1].args[0], "https://example-shop.test/img/new.jpg")
        self.assertTrue(first.closed and second.closed)

    def test_a_redirect_to_another_host_is_refused(self):
        with mock.patch("requests.get", return_value=FakeResponse(302, headers={"Location": "https://evil.example/x.jpg"})) as get:
            with self.assertRaises(ValueError):
                fetch_site_image(OG)
        self.assertEqual(get.call_count, 1)

    def test_a_missing_file_is_refused(self):
        with mock.patch("requests.get", return_value=FakeResponse(404)):
            with self.assertRaisesRegex(ImageSourceError, "404"):
                fetch_site_image(OG)

    def test_a_download_over_the_limit_is_refused_while_reading(self):
        with mock.patch.object(image_tools, "MAX_DOWNLOAD_BYTES", 100), \
                mock.patch("requests.get", return_value=FakeResponse(200, body=b"x" * 300)):
            with self.assertRaisesRegex(ImageSourceError, "larger than"):
                fetch_site_image(OG)

    def test_a_chain_of_redirects_is_cut_off(self):
        loop = [FakeResponse(302, headers={"Location": "/img/a.jpg"}) for _ in range(10)]
        with mock.patch("requests.get", side_effect=loop):
            with self.assertRaisesRegex(ImageSourceError, "redirects"):
                fetch_site_image(OG)


class ToolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(error_shield, "AUDIT_FILE", Path(tmp.name) / "audit.jsonl"),
                        mock.patch.dict(cloud_sink._state, {"started": True, "logger": None, "tracer": None}),
                        mock.patch.dict(os.environ, {"MCP_PREFLIGHT_REQUIRED": "0"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.data = picture(1200, 630)
        self.sha8 = hashlib.sha256(self.data).hexdigest()[:8]

    def make_client(self, existing=False, readback=True):
        state = {}
        client = mock.MagicMock()
        client.get_type.side_effect = lambda name: AssetOperation()
        client.enums = SimpleNamespace(AssetTypeEnum=AssetType, MimeTypeEnum=MimeType)

        def mutate(customer_id, operations):
            state["operation"] = operations[0]
            return SimpleNamespace(results=[SimpleNamespace(resource_name="customers/1/assets/555")])

        asset_service = mock.MagicMock()
        asset_service.mutate_assets.side_effect = mutate

        def search(customer_id, query):
            if "asset.name =" in query:
                return [SimpleNamespace(asset=SimpleNamespace(id=777, name="old"))] if existing else []
            if not readback:
                return []
            return [SimpleNamespace(asset=SimpleNamespace(
                id=555, name=state["operation"].create.name,
                image_asset=SimpleNamespace(full_size=SimpleNamespace(width_pixels=1200, height_pixels=630))))]

        ga_service = mock.MagicMock()
        ga_service.search.side_effect = search
        client.get_service.side_effect = lambda name: asset_service if name == "AssetService" else ga_service
        return client, asset_service, state

    def run_tool(self, client, **kwargs):
        req = CreateImageAssetRequest(source_url=kwargs.pop("source_url", OG), ratio=kwargs.pop("ratio", "LANDSCAPE"), **kwargs)
        with mock.patch("tools.image_tools.fetch_site_image", return_value=self.data), \
                mock.patch("tools.image_tools.get_ads_client", return_value=client):
            return create_image_asset(req, customer_id="1")

    def test_dry_run_checks_and_writes_nothing(self):
        client, asset_service, _ = self.make_client()
        result = self.run_tool(client, dry_run=True)
        self.assertTrue(result.success)
        self.assertIn("DRY RUN", result.data)
        self.assertIn("1200x630", result.data)
        asset_service.mutate_assets.assert_not_called()
        client.get_service.assert_not_called()

    def test_upload_sends_the_bytes_with_a_content_hash_in_the_name_and_reads_the_asset_back(self):
        client, asset_service, state = self.make_client()
        result = self.run_tool(client)
        asset = state["operation"].create
        self.assertTrue(result.success)
        self.assertEqual(AssetType(asset.type_).name, "IMAGE")
        self.assertEqual(asset.image_asset.data, self.data)
        self.assertEqual(asset.image_asset.file_size, len(self.data))
        self.assertEqual(MimeType(asset.image_asset.mime_type).name, "IMAGE_JPEG")
        self.assertEqual(asset.name, f"Shop site og-blumenwiese 1.91x1 {self.sha8}")
        self.assertIn("Image asset 555", result.data)
        self.assertIn("link_asset_to_entity", result.data)

    def test_a_custom_name_still_carries_the_hash(self):
        client, _, state = self.make_client()
        self.run_tool(client, name="Blumen Hero")
        self.assertEqual(state["operation"].create.name, f"Blumen Hero {self.sha8}")

    def test_the_same_picture_is_not_uploaded_twice(self):
        client, asset_service, _ = self.make_client(existing=True)
        result = self.run_tool(client)
        self.assertTrue(result.success)
        self.assertIn("already in the library", result.data)
        self.assertIn("777", result.data)
        asset_service.mutate_assets.assert_not_called()

    def test_an_unusable_source_is_a_typed_refusal_and_the_account_is_not_touched(self):
        client, asset_service, _ = self.make_client()
        req = CreateImageAssetRequest(source_url=OG, ratio="LANDSCAPE")
        with mock.patch("tools.image_tools.fetch_site_image", side_effect=ImageSourceError("answered HTTP 404")), \
                mock.patch("tools.image_tools.get_ads_client", return_value=client) as get_client:
            result = create_image_asset(req, customer_id="1")
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "IMAGE_SOURCE_REJECTED")
        get_client.assert_not_called()

    def test_an_asset_that_cannot_be_read_back_is_a_failure(self):
        client, _, _ = self.make_client(readback=False)
        result = self.run_tool(client)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "NOT_APPLIED")


if __name__ == "__main__":
    unittest.main()
