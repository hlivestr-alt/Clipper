import importlib.util
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from clipper_app.application.catalog import CatalogDatabase
from clipper_app.application.trends import (
    TrendRepository,
    TrendService,
    TrendServiceError,
    TikTokDiscoveryClient,
    TikTokDiscoveryError,
    _aggregate_pattern,
    _classify_tiktok_post,
    _probe_downloaded_video,
    normalize_hashtag_folder_name,
    _run_ytdlp,
    _sanitize_download_error,
    _validated_tiktok_video_url,
    trend_download_filename,
    trend_download_relative_path,
)
from trend_analyzer import _detect_cuts, _normalize_semantic_fields, _probe_media, _semantic_analysis, _visual_metrics


def _hashtag_payload(count=200):
    return {
        "code": 0,
        "request_id": "request-hashtags",
        "data": {
            "list": [
                {
                    "hashtag_id": f"h{index}",
                    "hashtag_name": f"trend{index}",
                    "rank_position": index,
                    "rank_change": "NEW" if index == 1 else "1",
                    "views": index * 1000,
                    "posts": index * 10,
                }
                for index in range(1, count + 1)
            ]
        },
    }


def _video_payload(groups=10, videos=20, hashtag_ids=None):
    group_ids = list(hashtag_ids) if hashtag_ids is not None else [f"h{index}" for index in range(1, groups + 1)]
    return {
        "code": 0,
        "request_id": f"request-videos-{group_ids[0]}-{group_ids[-1]}",
        "data": {
            "list": [
                {
                    "hashtag_id": hashtag_id,
                    "hashtag_name": f"trend{hashtag_id.removeprefix('h')}",
                    "top_video_list": [
                        {
                            "video_id": f"v{hashtag_id.removeprefix('h')}-{ordinal}",
                            "share_url": f"https://www.tiktok.com/@creator/video/v{hashtag_id.removeprefix('h')}-{ordinal}",
                            "embed_url": f"https://www.tiktok.com/player/v1/v{hashtag_id.removeprefix('h')}-{ordinal}",
                            "_post_metadata": {
                                "id_str": f"v{hashtag_id.removeprefix('h')}-{ordinal}",
                                "aweme_type": 0,
                                "video_info": {
                                    "meta": {"duration": 30, "format": "mp4"},
                                    "url_list": [f"https://v16-webapp-prime.tiktok.com/v{hashtag_id.removeprefix('h')}-{ordinal}.mp4"],
                                },
                            },
                        }
                        for ordinal in range(1, videos + 1)
                    ],
                }
                for hashtag_id in group_ids
            ]
        },
    }


class _FakeDiscoveryClient:
    def __init__(self):
        self.video_batches = []

    def trending_hashtags(self, **_kwargs):
        payload = _hashtag_payload()
        for item in payload["data"]["list"]:
            item["hashtag_name"] = f"skincaretrend{item['rank_position']}"
        return payload

    def trending_videos(self, hashtag_ids, **_kwargs):
        assert 1 <= len(hashtag_ids) <= 10
        self.video_batches.append(list(hashtag_ids))
        return _video_payload(hashtag_ids=hashtag_ids)


class _FakeOAuthService:
    def __init__(self):
        self.callbacks = []

    def status(self):
        return {
            "app_configured": True, "redirect_configured": True,
            "redirect_uri": "https://proyaofficial.com/callback", "callback_supported": True,
            "storage_encrypted": True, "connected": True, "authorization_required": False,
            "advertiser_ids": ["advertiser"], "selected_advertiser_id": "advertiser",
        }

    def authorization_url(self):
        return {
            "authorization_url": "https://business-api.tiktok.com/portal/auth?state=safe-state",
            "redirect_uri": "https://proyaofficial.com/callback", "expires_in": 600,
        }

    def exchange_callback(self, auth_code, state):
        self.callbacks.append((auth_code, state))
        return {"selected_advertiser_id": "advertiser"}

    def select_advertiser(self, advertiser_id):
        return {"selected_advertiser_id": advertiser_id, "advertiser_ids": [advertiser_id]}


class TrendServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.cfg = SimpleNamespace(
            WORKING_DIR=str(self.root / "working"),
            TREND_MEDIA_DIR=str(self.media),
            TREND_ANALYSIS_DIR=str(self.root / "analysis"),
            TREND_QWEN_ENABLED=False,
            VARIANTS_PER_CLIP=1,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK_FALLBACKS=[],
            SUBTITLE_FONT_DIR="assets/fonts",
            BGM_DIR=str(self.root / "bgm"),
        )
        self.database = CatalogDatabase(self.root / "clipper.sqlite3")
        self.discovery = _FakeDiscoveryClient()
        self.service = TrendService(self.database, self.cfg, client_factory=lambda: self.discovery)

    def tearDown(self):
        self.temp.cleanup()

    def test_refresh_persists_full_snapshot_and_ranked_video_ordinals(self):
        result = self.service.refresh(SimpleNamespace(
            country_code="ID", date_range="1DAY", category_name="ALL", top_hashtag_limit=10,
        ))

        self.assertEqual(result["hashtag_count"], 10)
        self.assertEqual(result["video_count"], 200)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM trend_hashtags"), 10)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM trend_videos"), 200)
        first = self.database.query(
            "SELECT provider_ordinal FROM trend_videos WHERE hashtag_id='h1' ORDER BY provider_ordinal LIMIT 1"
        )[0]
        self.assertEqual(first["provider_ordinal"], 1)
        serialized = json.dumps(self.service.page(category_name="ALL"))
        self.assertNotIn("access_token", serialized.casefold())
        self.assertNotIn("app_secret", serialized.casefold())

    def test_post_classifier_covers_video_image_carousel_unknown_and_unavailable(self):
        valid_video = _classify_tiktok_post({
            "video_id": "1",
            "_post_metadata": {
                "id_str": "1",
                "aweme_type": 0,
                "video_info": {
                    "meta": {"duration": 12, "format": "mp4"},
                    "url_list": ["https://v16-webapp-prime.tiktok.com/1.mp4"],
                },
            },
        })
        image = _classify_tiktok_post({
            "video_id": "2",
            "_post_metadata": {
                "id_str": "2", "aweme_type": 150,
                "image_post_info": {"images": [{"display_image": {}}]},
            },
        })
        carousel = _classify_tiktok_post({
            "video_id": "3",
            "_post_metadata": {
                "id_str": "3", "aweme_type": 150,
                "image_post_info": {"images": [{"display_image": {}}, {"display_image": {}}]},
            },
        })
        unknown = _classify_tiktok_post({
            "video_id": "4", "_post_metadata": {"id_str": "4", "aweme_type": 999},
        })
        unavailable = _classify_tiktok_post({
            "video_id": "5",
            "_post_metadata": {
                "status_code": 0,
                "items": [],
                "results": [{"id_str": "5", "code": "nil_core_data"}],
                "extra": {"fatal_item_ids": []},
            },
        })
        unplayable_video = _classify_tiktok_post({
            "video_id": "6",
            "_post_metadata": {
                "id_str": "6",
                "aweme_type": 0,
                "video_info": {"meta": {"duration": 20, "format": "mp4"}, "url_list": [""]},
            },
        })

        self.assertEqual((valid_video["media_type"], valid_video["is_available"]), ("video", True))
        self.assertEqual((image["media_type"], image["image_count"]), ("image", 1))
        self.assertEqual((carousel["media_type"], carousel["image_count"]), ("carousel", 2))
        self.assertEqual(unknown["exclusion_reason"], "unknown")
        self.assertEqual(unavailable["exclusion_reason"], "unavailable")
        self.assertEqual((unplayable_video["media_type"], unplayable_video["is_available"]), ("video", False))
        self.assertIn("nonempty_playable_urls=0", unplayable_video["classification_evidence"])

    def test_hashtag_normalization_and_ranked_path_generation_are_windows_safe(self):
        self.assertEqual(normalize_hashtag_folder_name("  #Moisturizer  "), "moisturizer")
        self.assertEqual(normalize_hashtag_folder_name(""), "unknown_hashtag")
        self.assertEqual(normalize_hashtag_folder_name(" #   "), "unknown_hashtag")
        self.assertEqual(normalize_hashtag_folder_name("CON"), "hashtag_con")
        self.assertEqual(normalize_hashtag_folder_name("prn.txt"), "hashtag_prn.txt")
        self.assertEqual(normalize_hashtag_folder_name("COM1"), "hashtag_com1")
        self.assertEqual(normalize_hashtag_folder_name("Glow-Up_日本"), "glow-up_日本")
        invalid = normalize_hashtag_folder_name('#Skin<Care>:"/\\|?*')
        self.assertFalse(any(character in invalid for character in '<>:"/\\|?*'))
        traversal = normalize_hashtag_folder_name("../Skincare")
        self.assertNotIn("/", traversal)
        self.assertNotIn("\\", traversal)
        self.assertNotIn("..", Path(traversal).parts)
        self.assertEqual(trend_download_filename(1, "7483920123456789012"), "001_7483920123456789012.mp4")
        self.assertEqual(trend_download_filename(20, "video-id"), "020_video-id.mp4")
        self.assertEqual(
            trend_download_relative_path("#Skincare", 12, "7484000123456789012"),
            "downloads/skincare/012_7484000123456789012.mp4",
        )

    def test_image_and_slideshow_candidates_are_excluded_before_final_ranking(self):
        hashtag_payload = _hashtag_payload(1)
        hashtag_payload["data"]["list"][0]["hashtag_name"] = "moisturizer"
        video_payload = _video_payload(groups=1, videos=5)
        video_payload["data"]["list"][0]["hashtag_name"] = "moisturizer"
        metadata = [
            {"id_str": "v1-1", "aweme_type": 150, "image_post_info": {"images": [{}]}},
            {
                "id_str": "v1-2", "video_info": {
                    "meta": {"duration": 10, "format": "mp4"},
                    "url_list": ["https://v16-webapp-prime.tiktok.com/v1-2.mp4"],
                },
            },
            {"id_str": "v1-3", "post_type": "slideshow"},
            {
                "id_str": "v1-4", "video_info": {
                    "meta": {"duration": 10, "format": "mp4"},
                    "url_list": ["https://v16-webapp-prime.tiktok.com/v1-4.mp4"],
                },
            },
            {
                "id_str": "v1-5", "video_info": {
                    "meta": {"duration": 10, "format": "mp4"},
                    "url_list": ["https://v16-webapp-prime.tiktok.com/v1-5.mp4"],
                },
            },
        ]
        for candidate, post_metadata in zip(
            video_payload["data"]["list"][0]["top_video_list"], metadata
        ):
            candidate["_post_metadata"] = post_metadata

        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=hashtag_payload, video_payload=video_payload,
        )
        rows = self.database.query(
            "SELECT video_id,provider_ordinal,final_rank,media_type FROM trend_videos "
            "WHERE snapshot_id=? ORDER BY provider_ordinal",
            (snapshot["snapshot_id"],),
        )
        self.assertEqual(
            [(row["video_id"], row["final_rank"]) for row in rows],
            [("v1-1", None), ("v1-2", 1), ("v1-3", None), ("v1-4", 2), ("v1-5", 3)],
        )
        page = self.service.page(category_name="ALL")
        self.assertEqual(
            [(video["video_id"], video["final_rank"]) for video in page["videos"]],
            [("v1-2", 1), ("v1-4", 2), ("v1-5", 3)],
        )
        self.assertEqual(len(page["videos"]), 3)

    def test_more_than_twenty_valid_candidates_are_capped_after_video_ranking(self):
        hashtag_payload = _hashtag_payload(1)
        hashtag_payload["data"]["list"][0]["hashtag_name"] = "skincare"
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=hashtag_payload, video_payload=_video_payload(groups=1, videos=22),
        )
        ranked = self.database.query(
            "SELECT video_id,final_rank FROM trend_videos "
            "WHERE snapshot_id=? AND final_rank IS NOT NULL ORDER BY final_rank",
            (snapshot["snapshot_id"],),
        )
        self.assertEqual(len(ranked), 20)
        self.assertEqual([row["final_rank"] for row in ranked], list(range(1, 21)))
        self.assertEqual(len(self.service.page(category_name="ALL")["videos"]), 20)

    def test_page_sends_only_playable_videos_with_visible_and_original_ranks_and_shortage_diagnostics(self):
        hashtag_payload = _hashtag_payload(1)
        hashtag_payload["data"]["list"][0]["hashtag_name"] = "moisturizer"
        metadata = [
            {"id_str": "v1-1", "aweme_type": 150, "image_post_info": {"images": [{}, {}]}},
            {"id_str": "v1-2", "aweme_type": 999},
            {
                "status_code": 0, "items": [],
                "results": [{"id_str": "v1-3", "code": "nil_core_data"}],
                "extra": {"fatal_item_ids": []},
            },
            {
                "id_str": "v1-4", "aweme_type": 0,
                "video_info": {
                    "meta": {"duration": 25, "format": "mp4"},
                    "url_list": ["https://v16-webapp-prime.tiktok.com/v1-4.mp4"],
                },
            },
            {"id_str": "v1-5", "aweme_type": 150, "image_post_info": {"images": [{}]}},
            {
                "id_str": "v1-6", "aweme_type": 0,
                "video_info": {"meta": {"duration": 30, "format": "mp4"}, "url_list": [""]},
            },
        ]
        video_payload = _video_payload(groups=1, videos=6)
        video_payload["data"]["list"][0]["hashtag_name"] = "moisturizer"
        for video, post_metadata in zip(video_payload["data"]["list"][0]["top_video_list"], metadata):
            video["_post_metadata"] = post_metadata

        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID",
            date_range="1DAY",
            category_name="ALL",
            hashtag_payload=hashtag_payload,
            video_payload=video_payload,
        )
        page = self.service.page(category_name="ALL")

        self.assertEqual(snapshot["candidate_count"], 6)
        self.assertEqual(snapshot["valid_video_count"], 1)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM trend_videos"), 6)
        self.assertEqual([video["video_id"] for video in page["videos"]], ["v1-4"])
        self.assertEqual(page["videos"][0]["final_rank"], 1)
        self.assertEqual(page["videos"][0]["original_provider_rank"], 4)
        self.assertEqual(page["videos"][0]["media_type"], "video")
        diagnostic = page["video_diagnostics"][0]
        self.assertEqual(diagnostic["hashtag_name"], "moisturizer")
        self.assertEqual(diagnostic["total_candidates_returned"], 6)
        self.assertEqual(diagnostic["video_posts_detected"], 2)
        self.assertEqual(diagnostic["image_carousel_posts_excluded"], 2)
        self.assertEqual(diagnostic["unknown_posts_excluded"], 1)
        self.assertEqual(diagnostic["unavailable_posts_excluded"], 2)
        self.assertEqual(diagnostic["valid_videos_stored"], 1)
        self.assertEqual(diagnostic["sent_to_frontend"], 1)
        self.assertFalse(diagnostic["pagination_available"])
        self.assertEqual(diagnostic["candidates"][3]["original_tiktok_rank"], 4)
        self.assertIn("item.video_info", diagnostic["candidates"][3]["classification_evidence"])

    def test_refresh_caps_relevant_hashtags_at_thirty_and_batches_video_requests(self):
        result = self.service.refresh(SimpleNamespace(
            country_code="ID", date_range="1DAY", category_name="ALL", top_hashtag_limit=30,
        ))

        self.assertEqual(result["hashtag_count"], 30)
        self.assertEqual(result["video_count"], 600)
        self.assertEqual(result["video_batch_count"], 3)
        self.assertEqual([len(batch) for batch in self.discovery.video_batches], [10, 10, 10])
        self.assertEqual(self.discovery.video_batches[0], [f"h{index}" for index in range(1, 11)])
        self.assertEqual(self.discovery.video_batches[1], [f"h{index}" for index in range(11, 21)])
        self.assertEqual(self.discovery.video_batches[2], [f"h{index}" for index in range(21, 31)])
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM trend_videos"), 600)
        snapshot_payload = json.loads(self.database.query("SELECT payload_json FROM trend_snapshots")[0]["payload_json"])
        self.assertEqual(len(snapshot_payload["provider_request_ids"]), 4)
        self.assertEqual(snapshot_payload["hashtag_filter"]["stored"], 30)
        page = self.service.page(category_name="ALL")
        self.assertEqual(len(page["hashtags"]), 30)
        self.assertEqual(page["hashtag_diagnostics"]["backend_returned"], 30)
        self.assertEqual([item["display_rank"] for item in page["hashtags"]], list(range(1, 31)))

    def test_refresh_fetches_videos_only_for_relevant_hashtags_and_reports_exclusions(self):
        payload = _hashtag_payload(5)
        for item, name in zip(
            payload["data"]["list"],
            ["WorldCupFootball", "acne", "EasyDinnerRecipe", "niacinamide", "mysterytopic"],
        ):
            item["hashtag_name"] = name

        class MixedDiscoveryClient:
            def __init__(self):
                self.video_batches = []

            def trending_hashtags(self, **_kwargs):
                return payload

            def trending_videos(self, hashtag_ids, **_kwargs):
                self.video_batches.append(list(hashtag_ids))
                return _video_payload(hashtag_ids=hashtag_ids)

        discovery = MixedDiscoveryClient()
        service = TrendService(self.database, self.cfg, client_factory=lambda: discovery)
        result = service.refresh(SimpleNamespace(
            country_code="ID", date_range="1DAY", category_name="ALL", top_hashtag_limit=20,
        ))

        self.assertEqual(discovery.video_batches, [["h2", "h4"]])
        self.assertEqual(result["hashtag_count"], 2)
        self.assertEqual(result["hashtag_filter"]["total_retrieved"], 5)
        self.assertEqual(result["hashtag_filter"]["classified_relevant"], 2)
        self.assertEqual(result["hashtag_filter"]["accepted_topical"], 2)
        self.assertEqual(result["hashtag_filter"]["accepted_brands"], 0)
        self.assertEqual(result["hashtag_filter"]["excluded"], 3)
        self.assertEqual(result["hashtag_filter"]["deduplicated"], 0)
        self.assertEqual(result["hashtag_filter"]["requested"], 200)
        self.assertFalse(result["hashtag_filter"]["request_count_parameter_supported"])
        self.assertFalse(result["hashtag_filter"]["pagination"]["supported"])
        self.assertEqual(len(result["hashtag_filter"]["classifications"]), 5)
        self.assertEqual(
            {item["category"] for item in result["hashtag_filter"]["exclusions"]},
            {"sports", "food", "unclassified"},
        )
        rows = self.database.query(
            "SELECT hashtag_id,rank_position,original_rank,display_rank,source,source_category,"
            "relevance_type,classification_reason FROM trend_hashtags ORDER BY display_rank"
        )
        self.assertEqual([(row["hashtag_id"], row["rank_position"]) for row in rows], [("h2", 2), ("h4", 4)])
        self.assertEqual([row["display_rank"] for row in rows], [1, 2])
        self.assertEqual({row["source_category"] for row in rows}, {"ALL"})

    def test_media_link_is_allowlisted_hashed_and_rejects_traversal(self):
        result = self.service.refresh(SimpleNamespace(
            country_code="ID", date_range="1DAY", category_name="ALL", top_hashtag_limit=10,
        ))
        clip = self.media / "approved.mp4"
        clip.write_bytes(b"permission-cleared-video")
        with mock.patch("clipper_app.application.trends._validate_video"):
            linked = self.service.link_media("v1-1", "approved.mp4", "operator:test")
        self.assertEqual(linked["status"], "media_ready")
        self.assertEqual(len(linked["file_sha256"]), 64)
        with self.assertRaises(TrendServiceError):
            self.service.link_media("v1-1", "../outside.mp4", "operator:test")
        self.assertEqual(result["hashtag_count"], 10)

    def test_bulk_download_keeps_per_hashtag_copies_and_reuses_ranked_files(self):
        hashtag_payload = _hashtag_payload(2)
        for item in hashtag_payload["data"]["list"]:
            item["hashtag_name"] = f"skincare{item['rank_position']}"
        video_payload = _video_payload(groups=2, videos=1)
        video_payload["data"]["list"][1]["top_video_list"][0] = dict(
            video_payload["data"]["list"][0]["top_video_list"][0]
        )
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=hashtag_payload, video_payload=video_payload,
        )
        calls = []

        def fake_download(video_id, source_url, target_dir, timeout):
            calls.append((video_id, source_url, timeout))
            path = target_dir / f"{video_id}.mp4"
            path.write_bytes(b"downloaded-media")
            return path

        service = TrendService(self.database, self.cfg, client_factory=lambda: self.discovery, download_runner=fake_download)
        service._ytdlp_cache = (True, "2026.07.04")
        request = SimpleNamespace(snapshot_id=snapshot["snapshot_id"], rights_confirmed=True, retry_failed=True)
        with mock.patch("clipper_app.application.trends._probe_downloaded_video", return_value={"duration_seconds": 3.5}):
            first = service.download_all(request, run_id="run-one", actor="operator:test")
            self.database.execute("DELETE FROM trend_media_links WHERE video_id='v1-1'")
            second = service.download_all(request, run_id="run-two", actor="operator:test")

        self.assertEqual(first["downloaded_count"], 2)
        self.assertEqual(second["reused_count"], 2)
        self.assertEqual(second["approved_count"], 2)
        self.assertEqual(len(calls), 2)
        row = TrendRepository(self.database).download(snapshot["snapshot_id"], "h1", "v1-1")
        self.assertEqual(row["status"], "downloaded")
        self.assertEqual(row["attempt_count"], 2)
        self.assertTrue((self.media / "downloads" / "skincare1" / "001_v1-1.mp4").is_file())
        self.assertTrue((self.media / "downloads" / "skincare2" / "001_v1-1.mp4").is_file())
        link = TrendRepository(self.database).media_link("v1-1")
        self.assertEqual(link["status"], "media_ready")
        self.assertEqual(link["approved_by"], "operator:test")
        page_video = service.page(category_name="ALL")["videos"][0]
        self.assertEqual(page_video["download_status"], "downloaded")
        self.assertEqual(page_video["media_status"], "media_ready")

    def test_successful_refresh_reconciles_ranks_stale_files_metadata_and_database(self):
        hashtag_payload = _hashtag_payload(1)
        hashtag_payload["data"]["list"][0]["hashtag_name"] = "#Moisturizer"
        video_payload = _video_payload(groups=1, videos=3)
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=hashtag_payload, video_payload=video_payload,
        )
        self.database.execute(
            "UPDATE trend_videos SET share_url='' WHERE snapshot_id=? AND video_id='v1-1'",
            (snapshot["snapshot_id"],),
        )
        folder = self.media / "downloads" / "moisturizer"
        folder.mkdir(parents=True)
        (folder / "003_v1-1.mp4").write_bytes(b"old-rank")
        (folder / "002_v1-2.mp4").write_bytes(b"correct-rank")
        (folder / "020_stale.mp4").write_bytes(b"stale")
        calls = []

        def fake_download(video_id, _source_url, target_dir, _timeout):
            calls.append(video_id)
            path = target_dir / f"{video_id}.mp4"
            path.write_bytes(b"new-video")
            return path

        service = TrendService(self.database, self.cfg, download_runner=fake_download)
        service._ytdlp_cache = (True, "2026.07.04")
        with mock.patch(
            "clipper_app.application.trends._probe_downloaded_video",
            return_value={"duration_seconds": 4.0},
        ):
            result = service.download_all(SimpleNamespace(
                snapshot_id=snapshot["snapshot_id"], rights_confirmed=True, retry_failed=True,
            ))

        self.assertEqual(result["downloaded_count"], 1)
        self.assertEqual(result["reused_count"], 2)
        self.assertEqual(calls, ["v1-3"])
        self.assertEqual(
            sorted(path.name for path in folder.iterdir()),
            ["001_v1-1.mp4", "002_v1-2.mp4", "003_v1-3.mp4", "metadata.json"],
        )
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["hashtag"], "moisturizer")
        self.assertEqual(
            [(item["rank"], item["filename"], item["relative_path"]) for item in metadata["videos"]],
            [
                (1, "001_v1-1.mp4", "moisturizer/001_v1-1.mp4"),
                (2, "002_v1-2.mp4", "moisturizer/002_v1-2.mp4"),
                (3, "003_v1-3.mp4", "moisturizer/003_v1-3.mp4"),
            ],
        )
        database_rows = self.database.query(
            "SELECT video_id,final_rank,relative_path,status FROM trend_media_downloads "
            "WHERE snapshot_id=? ORDER BY final_rank",
            (snapshot["snapshot_id"],),
        )
        self.assertEqual(
            [
                (row["video_id"], row["final_rank"], row["relative_path"], row["status"])
                for row in database_rows
            ],
            [
                ("v1-1", 1, "downloads/moisturizer/001_v1-1.mp4", "downloaded"),
                ("v1-2", 2, "downloads/moisturizer/002_v1-2.mp4", "downloaded"),
                ("v1-3", 3, "downloads/moisturizer/003_v1-3.mp4", "downloaded"),
            ],
        )
        page = service.page(category_name="ALL")
        self.assertEqual(
            [
                (video["final_rank"], Path(video["downloaded_relative_path"]).name)
                for video in page["videos"]
            ],
            [(1, "001_v1-1.mp4"), (2, "002_v1-2.mp4"), (3, "003_v1-3.mp4")],
        )

    def test_download_url_validation_and_error_sanitization(self):
        canonical = _validated_tiktok_video_url(
            "https://www.tiktok.com/@creator/video/123?token=secret", "123"
        )
        self.assertEqual(canonical, "https://www.tiktok.com/@creator/video/123")
        for value in (
            "http://www.tiktok.com/@creator/video/123",
            "https://example.com/@creator/video/123",
            "https://www.tiktok.com/@creator/video/456",
        ):
            with self.assertRaises(TrendServiceError):
                _validated_tiktok_video_url(value, "123")
        sanitized = _sanitize_download_error("ERROR https://host/path?token=secret cookie=abc")
        self.assertNotIn("https://", sanitized)
        self.assertNotIn("abc", sanitized)
        self.assertIn("[redacted]", sanitized)

    def test_ytdlp_runner_uses_module_without_shell_or_browser_credentials(self):
        target = self.media / "downloads" / "123"
        target.mkdir(parents=True)
        produced = target / "123.mp4"
        produced.write_bytes(b"media")
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch("clipper_app.application.trends.subprocess.run", return_value=completed) as run:
            result = _run_ytdlp("123", "https://www.tiktok.com/@creator/video/123", target, 600)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [mock.ANY, "-m", "yt_dlp"])
        self.assertIn("--ignore-config", command)
        self.assertNotIn("--cookies-from-browser", command)
        self.assertNotIn("--proxy", command)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(result, produced)

    def test_startup_marks_inflight_downloads_interrupted(self):
        snapshot = self.service.refresh(SimpleNamespace(
            country_code="ID", date_range="1DAY", category_name="ALL", top_hashtag_limit=1,
        ))
        repository = TrendRepository(self.database)
        repository.queue_download(
            video_id="v1-1", snapshot_id=snapshot["snapshot_id"], hashtag_id="h1",
            hashtag_name="skincaretrend1", normalized_hashtag="skincaretrend1", final_rank=1,
            run_id="stale",
            source_url="https://www.tiktok.com/@creator/video/v1-1",
        )
        TrendService(self.database, self.cfg, client_factory=lambda: self.discovery)
        self.assertEqual(
            repository.download(snapshot["snapshot_id"], "h1", "v1-1")["status"],
            "interrupted",
        )

    def test_catalog_migrates_legacy_global_download_rows_without_moving_files(self):
        legacy_path = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE trend_media_downloads (
                video_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                relative_path TEXT,
                file_sha256 TEXT,
                file_size INTEGER,
                file_mtime_ns INTEGER,
                duration_seconds REAL,
                status TEXT NOT NULL,
                error TEXT,
                extractor_version TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO trend_media_downloads(
                video_id,snapshot_id,run_id,source_url,relative_path,status,attempt_count,updated_at
            ) VALUES(
                'legacy-video','legacy-snapshot','legacy-run','https://www.tiktok.com/',
                'downloads/legacy-video/legacy-video.mp4','downloaded',1,'2026-01-01T00:00:00Z'
            );
            """
        )
        connection.close()

        migrated = CatalogDatabase(legacy_path)
        migrated.ensure_schema()
        primary_key = [
            row["name"]
            for row in sorted(
                migrated.query("PRAGMA table_info(trend_media_downloads)"),
                key=lambda row: row["pk"] or 999,
            )
            if row["pk"]
        ]
        self.assertEqual(primary_key, ["snapshot_id", "hashtag_id", "video_id"])
        row = migrated.query("SELECT * FROM trend_media_downloads")[0]
        self.assertEqual(row["video_id"], "legacy-video")
        self.assertEqual(row["hashtag_id"], "")
        self.assertEqual(row["relative_path"], "downloads/legacy-video/legacy-video.mp4")

    def test_download_queue_tolerates_a_long_running_process_during_identity_migration(self):
        transitional_path = self.root / "transitional.sqlite3"
        connection = sqlite3.connect(transitional_path)
        connection.executescript(
            """
            CREATE TABLE trend_media_downloads (
                snapshot_id TEXT NOT NULL,
                hashtag_id TEXT NOT NULL,
                video_id TEXT PRIMARY KEY,
                hashtag_name TEXT NOT NULL,
                normalized_hashtag TEXT NOT NULL,
                final_rank INTEGER,
                run_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.close()
        transitional = CatalogDatabase(transitional_path)
        transitional._migrated = True
        repository = TrendRepository(transitional)
        repository.queue_download(
            snapshot_id="snapshot",
            hashtag_id="hashtag",
            video_id="video",
            hashtag_name="Skincare",
            normalized_hashtag="skincare",
            final_rank=1,
            run_id="run",
            source_url="https://www.tiktok.com/@creator/video/video",
        )
        row = transitional.query("SELECT * FROM trend_media_downloads")[0]
        self.assertEqual((row["snapshot_id"], row["hashtag_id"], row["video_id"]), (
            "snapshot", "hashtag", "video",
        ))

    def test_refresh_refuses_to_overwrite_a_colliding_legacy_video_id_folder(self):
        hashtag_payload = _hashtag_payload(1)
        hashtag_payload["data"]["list"][0]["hashtag_name"] = "skincare123"
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=hashtag_payload, video_payload=_video_payload(groups=1, videos=1),
        )
        legacy_folder = self.media / "downloads" / "skincare123"
        legacy_folder.mkdir(parents=True)
        legacy_file = legacy_folder / "skincare123.mp4"
        legacy_file.write_bytes(b"legacy")
        service = TrendService(self.database, self.cfg)
        service._ytdlp_cache = (True, "2026.07.04")
        with self.assertRaisesRegex(TrendServiceError, "collides with a legacy"):
            service.download_all(SimpleNamespace(
                snapshot_id=snapshot["snapshot_id"], rights_confirmed=True, retry_failed=True,
            ))
        self.assertEqual(legacy_file.read_bytes(), b"legacy")

    def test_partial_hashtag_refresh_keeps_successes_and_compacts_ranks(self):
        hashtag_payload = _hashtag_payload(1)
        hashtag_payload["data"]["list"][0]["hashtag_name"] = "skincare"
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=hashtag_payload, video_payload=_video_payload(groups=1, videos=3),
        )
        lock = threading.Lock()
        active = 0
        maximum = 0

        def fake_download(video_id, _source_url, target_dir, _timeout):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            if video_id == "v1-2":
                raise TrendServiceError("private video https://signed.example/path?cookie=bad")
            path = target_dir / f"{video_id}.mp4"
            path.write_bytes(video_id.encode("ascii"))
            return path

        service = TrendService(self.database, self.cfg, download_runner=fake_download)
        service._ytdlp_cache = (True, "2026.07.04")
        previous = self.media / "downloads" / "skincare"
        previous.mkdir(parents=True)
        (previous / "001_previous.mp4").write_bytes(b"previous")
        (previous / "metadata.json").write_text('{"previous": true}', encoding="utf-8")
        with mock.patch("clipper_app.application.trends._probe_downloaded_video", return_value={"duration_seconds": 1.0}):
            result = service.download_all(SimpleNamespace(
                snapshot_id=snapshot["snapshot_id"], rights_confirmed=True, retry_failed=True,
            ))
        self.assertEqual(maximum, 2)
        self.assertEqual(result["approved_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(
            sorted(path.name for path in previous.iterdir()),
            ["001_v1-1.mp4", "002_v1-3.mp4", "metadata.json"],
        )
        metadata = json.loads((previous / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [(item["rank"], item["video_id"], item["relative_path"]) for item in metadata["videos"]],
            [
                (1, "v1-1", "skincare/001_v1-1.mp4"),
                (2, "v1-3", "skincare/002_v1-3.mp4"),
            ],
        )
        ranked = TrendRepository(self.database).videos(snapshot["snapshot_id"])
        self.assertEqual(
            [(row["video_id"], row["final_rank"]) for row in ranked],
            [("v1-1", 1), ("v1-3", 2)],
        )
        failed = TrendRepository(self.database).download(snapshot["snapshot_id"], "h1", "v1-2")
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("https://", failed["error"])
        self.assertNotIn("bad", failed["error"])

    def test_all_failed_hashtag_refresh_preserves_previous_folder(self):
        hashtag_payload = _hashtag_payload(1)
        hashtag_payload["data"]["list"][0]["hashtag_name"] = "skincare"
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=hashtag_payload, video_payload=_video_payload(groups=1, videos=2),
        )

        def fail_download(*_args):
            raise TrendServiceError("extractor failed")

        service = TrendService(self.database, self.cfg, download_runner=fail_download)
        service._ytdlp_cache = (True, "2026.07.04")
        previous = self.media / "downloads" / "skincare"
        previous.mkdir(parents=True)
        (previous / "001_previous.mp4").write_bytes(b"previous")
        (previous / "metadata.json").write_text('{"previous": true}', encoding="utf-8")
        with self.assertRaisesRegex(TrendServiceError, "No TikTok videos"):
            service.download_all(SimpleNamespace(
                snapshot_id=snapshot["snapshot_id"], rights_confirmed=True, retry_failed=True,
            ))
        self.assertEqual(
            sorted(path.name for path in previous.iterdir()),
            ["001_previous.mp4", "metadata.json"],
        )
        self.assertEqual(
            json.loads((previous / "metadata.json").read_text(encoding="utf-8")),
            {"previous": True},
        )

    def test_hashtag_refresh_reuses_valid_legacy_video_id_files(self):
        hashtag_payload = _hashtag_payload(1)
        hashtag_payload["data"]["list"][0]["hashtag_name"] = "skincare"
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=hashtag_payload, video_payload=_video_payload(groups=1, videos=2),
        )
        downloads = self.media / "downloads"
        for video_id in ("v1-1", "v1-2"):
            legacy = downloads / video_id
            legacy.mkdir(parents=True)
            (legacy / f"{video_id}.mp4").write_bytes(video_id.encode("ascii"))

        def unexpected_download(*_args):
            raise AssertionError("valid legacy files should be reused")

        service = TrendService(self.database, self.cfg, download_runner=unexpected_download)
        service._ytdlp_cache = (True, "2026.07.04")
        with mock.patch(
            "clipper_app.application.trends._probe_downloaded_video",
            return_value={"duration_seconds": 1.0},
        ):
            result = service.download_all(SimpleNamespace(
                snapshot_id=snapshot["snapshot_id"], rights_confirmed=True, retry_failed=True,
            ))
        self.assertEqual(result["reused_count"], 2)
        self.assertEqual(
            sorted(path.name for path in (downloads / "skincare").iterdir()),
            ["001_v1-1.mp4", "002_v1-2.mp4", "metadata.json"],
        )
        self.assertTrue((downloads / "v1-1" / "v1-1.mp4").is_file())
        self.assertTrue((downloads / "v1-2" / "v1-2.mp4").is_file())

    def test_media_probe_accepts_a_valid_mp4_without_an_audio_stream(self):
        probe_result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "format": {"duration": "3.5", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                "streams": [{"codec_type": "video"}],
            }),
            stderr="",
        )
        with mock.patch("clipper_app.application.trends.subprocess.run", return_value=probe_result):
            result = _probe_downloaded_video(self.root / "silent.mp4")
        self.assertEqual(result["duration_seconds"], 3.5)
        self.assertEqual(result["stream_types"], ["video"])

    def test_aggregation_applies_only_supported_high_confidence_fields(self):
        fingerprints = []
        for index in range(5):
            recommendations = {
                "hook_type": {"value": "text", "confidence": 0.9},
                "subtitle_position": {"value": "center", "confidence": 0.8},
                "letterbox_enabled": {"value": False, "confidence": 0.9},
                "letterbox_top_frac": {"value": 0.08 + index * 0.01, "confidence": 0.9},
            }
            if index == 4:
                recommendations["hook_type"] = {"value": "none", "confidence": 0.9}
                recommendations["subtitle_position"] = {"value": "bottom", "confidence": 0.8}
            fingerprints.append({"fingerprint_id": f"fp{index}", "recommendations": recommendations})

        active = {
            "schema_version": 1,
            "revision": "base-revision",
            "variants": [{"hook_type": "none", "subtitle_position": "bottom", "font_id": "keep-font"}],
        }
        with mock.patch("variation_profile.load_active_profile", return_value=active), mock.patch(
            "variation_profile.profile_revision", return_value="suggested-revision"
        ):
            pattern = _aggregate_pattern("snapshot", fingerprints, self.cfg)

        self.assertTrue(pattern["recommendations"]["hook_type"]["applied_to_suggestion"])
        self.assertEqual(pattern["suggested_profile"]["variants"][0]["hook_type"], "text")
        self.assertEqual(pattern["suggested_profile"]["variants"][0]["font_id"], "keep-font")
        self.assertEqual(pattern["suggested_profile"]["variants"][0]["letterbox_top_frac"], 0.1)
        self.assertFalse(pattern["recommendations"]["subtitle_position"]["applied_to_suggestion"])
        self.assertEqual(pattern["suggested_profile"]["variants"][0]["subtitle_position"], "bottom")

    def test_sample_validation_rejects_single_hashtag_before_analysis(self):
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=_hashtag_payload(1), video_payload=_video_payload(groups=1, videos=5),
        )
        with self.assertRaisesRegex(TrendServiceError, "three hashtags"):
            self.service.analyze(SimpleNamespace(
                snapshot_id=snapshot["snapshot_id"],
                video_ids=[f"v1-{index}" for index in range(1, 6)],
                force=False,
            ))

    def test_analysis_orchestrates_ten_videos_and_persists_read_only_pattern(self):
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=_hashtag_payload(3), video_payload=_video_payload(groups=3, videos=4),
        )
        video_ids = ["v1-1", "v1-2", "v1-3", "v1-4", "v2-1", "v2-2", "v2-3", "v3-1", "v3-2", "v3-3"]
        with mock.patch("clipper_app.application.trends._validate_video"):
            for video_id in video_ids:
                path = self.media / f"{video_id}.mp4"
                path.write_bytes(video_id.encode("ascii"))
                self.service.link_media(video_id, path.name, "operator:test")

        def fake_analyze(video_id, _path, file_hash, _cfg):
            return {
                "video_id": video_id,
                "file_sha256": file_hash,
                "recommendations": {
                    "hook_type": {"value": "text", "confidence": 0.9},
                    "letterbox_enabled": {"value": False, "confidence": 0.9},
                },
                "warnings": ["Qwen-VL unavailable; deterministic fingerprint retained"],
            }

        active = {"schema_version": 1, "revision": "active", "variants": [{"hook_type": "none", "font_id": "keep"}]}
        with mock.patch("clipper_app.application.trends.analyze_trend_video", side_effect=fake_analyze), mock.patch(
            "variation_profile.load_active_profile", return_value=active
        ), mock.patch("variation_profile.profile_revision", return_value="suggested"):
            result = self.service.analyze(SimpleNamespace(snapshot_id=snapshot["snapshot_id"], video_ids=video_ids, force=False))

        self.assertEqual(result["analyzed_count"], 10)
        self.assertEqual(result["failed_count"], 0)
        pattern = self.service.pattern(result["pattern_id"])
        self.assertEqual(pattern["sample_count"], 10)
        self.assertEqual(pattern["suggested_profile"]["variants"][0]["hook_type"], "text")
        self.assertEqual(pattern["suggested_profile"]["variants"][0]["font_id"], "keep")
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM trend_fingerprints"), 10)

    def test_fingerprint_cache_is_bound_to_media_hash_and_analyzer_version(self):
        snapshot = TrendRepository(self.database).save_snapshot(
            country_code="ID", date_range="1DAY", category_name="ALL",
            hashtag_payload=_hashtag_payload(1), video_payload=_video_payload(groups=1, videos=1),
        )
        repository = TrendRepository(self.database)
        repository.save_fingerprint(snapshot["snapshot_id"], "v1-1", "h1", "hash-one", "completed", {
            "video_id": "v1-1", "recommendations": {},
        })
        self.assertIsNotNone(repository.cached_fingerprint(snapshot["snapshot_id"], "v1-1", "hash-one"))
        self.assertIsNone(repository.cached_fingerprint(snapshot["snapshot_id"], "v1-1", "hash-two"))

    def test_semantic_normalization_rejects_unbounded_labels(self):
        normalized = _normalize_semantic_fields({
            "hook_type": {"value": "text", "confidence": 2, "evidence": "visible text"},
            "color_grade": {"value": "invented", "confidence": 1},
            "subtitle_enabled": {"value": True, "confidence": 0.75},
        })
        self.assertEqual(normalized["hook_type"]["confidence"], 1.0)
        self.assertNotIn("color_grade", normalized)
        self.assertTrue(normalized["subtitle_enabled"]["value"])

    def test_qwen_failure_returns_deterministic_fallback_warning(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy is unavailable")
        warnings = []
        cfg = SimpleNamespace(TREND_QWEN_ENABLED=True, TREND_QWEN_CONTACT_SHEET_MAX_FRAMES=2)
        with mock.patch("openai.OpenAI", side_effect=RuntimeError("offline")):
            result = _semantic_analysis([np.zeros((32, 18, 3), dtype=np.uint8)] * 2, cfg, warnings)
        self.assertFalse(result["available"])
        self.assertTrue(any("deterministic fingerprint retained" in warning for warning in warnings))

    def test_legacy_semantic_analyzer_rejects_non_loopback_endpoint(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy is unavailable")
        warnings = []
        cfg = SimpleNamespace(TREND_QWEN_ENABLED=True, SCORER_VISION_BASE_URL="https://api.example.com/v1")
        result = _semantic_analysis([np.zeros((8, 8, 3), dtype=np.uint8)], cfg, warnings)
        self.assertFalse(result["available"])
        self.assertTrue(any("loopback" in warning for warning in warnings))

    def test_discovery_errors_redact_access_token_and_app_secret(self):
        client = TikTokDiscoveryClient("sensitive-token", "advertiser")
        response = requests.Response()
        response.status_code = 400
        response._content = json.dumps(
            {"code": 40001, "message": "bad sensitive-token exposed-secret"}
        ).encode("utf-8")
        with mock.patch.dict("os.environ", {"TIKTOK_APP_SECRET": "exposed-secret"}), mock.patch(
            "requests.get", return_value=response
        ):
            with self.assertRaises(TikTokDiscoveryError) as caught:
                client.trending_hashtags(country_code="ID", date_range="1DAY", category_name="ALL")
        message = str(caught.exception)
        self.assertNotIn("sensitive-token", message)
        self.assertNotIn("exposed-secret", message)
        self.assertIn("[redacted]", message)

    def test_discovery_transport_error_explains_tls_failure_without_leaking_url(self):
        client = TikTokDiscoveryClient("sensitive-token", "advertiser", timeout=12)
        error = requests.exceptions.SSLError(
            "certificate failure for https://business-api.tiktok.com/?access_token=sensitive-token"
        )
        with mock.patch("requests.get", side_effect=error):
            with self.assertRaises(TikTokDiscoveryError) as caught:
                client.trending_hashtags(country_code="ID", date_range="1DAY", category_name="ALL")

        message = str(caught.exception)
        self.assertIn("secure TLS connection", message)
        self.assertNotIn("sensitive-token", message)
        self.assertNotIn("https://", message)

    def test_discovery_client_rejects_more_than_ten_hashtags_per_provider_request(self):
        client = TikTokDiscoveryClient("sensitive-token", "advertiser")
        with self.assertRaisesRegex(TikTokDiscoveryError, "between 1 and 10"):
            client.trending_videos([f"h{index}" for index in range(11)], country_code="ID", date_range="1DAY")

    def test_player_metadata_request_uses_item_id_and_returns_explicit_post_fields(self):
        client = TikTokDiscoveryClient("sensitive-token", "advertiser")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({
            "status_code": 0,
            "items": [{
                "id_str": "7653459400597753108",
                "aweme_type": 150,
                "image_post_info": {"images": [{}, {}]},
            }],
            "results": [{"id_str": "7653459400597753108", "code": "ok"}],
            "extra": {"fatal_item_ids": []},
        }).encode("utf-8")
        with mock.patch("requests.get", return_value=response) as get:
            payload = client.post_metadata("7653459400597753108")

        self.assertEqual(payload["items"][0]["aweme_type"], 150)
        self.assertEqual(len(payload["items"][0]["image_post_info"]["images"]), 2)
        self.assertTrue(get.call_args.args[0].endswith("/player/api/v1/items"))
        self.assertEqual(get.call_args.kwargs["params"]["item_ids"], "7653459400597753108")
        self.assertNotIn("Access-Token", get.call_args.kwargs["headers"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_synthetic_video_probe_cuts_and_letterbox_metrics(self):
        clip = self.root / "synthetic.mp4"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=320x240:d=1:r=30",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=1:r=30",
            "-filter_complex", "[0:v]drawbox=y=0:h=24:color=black:t=fill,drawbox=y=216:h=24:color=black:t=fill[v0];[1:v]drawbox=y=0:h=24:color=black:t=fill,drawbox=y=216:h=24:color=black:t=fill[v1];[v0][v1]concat=n=2:v=1:a=0,format=yuv420p[out]",
            "-map", "[out]", str(clip),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        media = _probe_media(clip)
        cuts = _detect_cuts(clip)
        visual, _frames = _visual_metrics(clip, media["duration_seconds"])
        self.assertEqual((media["width"], media["height"]), (320, 240))
        self.assertTrue(any(0.8 <= cut <= 1.2 for cut in cuts), cuts)
        self.assertGreater(visual["letterbox_top_frac"], 0.07)
        self.assertGreater(visual["letterbox_bottom_frac"], 0.07)


@unittest.skipIf(importlib.util.find_spec("fastapi") is None, "fastapi is not installed")
class TrendApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from clipper_app.application.api_security import ApiSecuritySettings
        from clipper_app.application.control_services import ControlJobService, SettingsService
        from clipper_app.application.read_services import ReadDashboardService
        from clipper_app.application.settings import LegacyConfigProvider
        from clipper_app.web_api import create_app

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        for name in ("working", "output", "vods", "modules", "media", "bgm"):
            (root / name).mkdir()
        state = root / "working" / "state.json"
        state.write_text(json.dumps({"schema_version": 2, "queue_status": "idle", "videos": {}}), encoding="utf-8")
        cfg = SimpleNamespace(
            OUTPUT_DIR=str(root / "output"), WORKING_DIR=str(root / "working"), QUEUE_INPUT_DIR=str(root / "vods"),
            QUEUE_STATE_FILE=str(state), QUEUE_CONTROL_FILE=str(root / "working" / "control.json"),
            QUEUE_FOREVER_STATE_FILE=str(root / "working" / "forever.json"), QUEUE_STAGE_ADMISSION_LIMIT=3,
            MODULE_LIBRARY_DIR=str(root / "modules"), QUEUE_DASHBOARD_RUNNING_STALL_SECONDS=7200.0,
            QUEUE_DASHBOARD_QUEUED_STALL_SECONDS=86400.0, VARIANTS_PER_CLIP=1,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf", FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK_FALLBACKS=[], SUBTITLE_FONT_DIR="assets/fonts", BGM_DIR=str(root / "bgm"),
            TREND_MEDIA_DIR=str(root / "media"), TREND_ANALYSIS_DIR=str(root / "analysis"), TREND_QWEN_ENABLED=False,
        )
        service = ReadDashboardService(LegacyConfigProvider(cfg))
        security = ApiSecuritySettings(
            token="trend-test-token", actor="desktop:trend-test", desktop=False,
            allowed_hosts=("testserver", "127.0.0.1", "localhost"), allowed_origins=("http://127.0.0.1:5173",),
        )
        with mock.patch("clipper_app.application.trends.TikTokDiscoveryClient.from_oauth_service", return_value=_FakeDiscoveryClient()):
            self.oauth = _FakeOAuthService()
            app = create_app(
                service,
                job_service=ControlJobService(cfg, run_async=False),
                settings_service=SettingsService(service.settings_provider),
                security_settings=security,
                tiktok_oauth_service=self.oauth,
            )
        self.client = TestClient(app, headers={"Authorization": "Bearer trend-test-token"})
        self.public = TestClient(app)
        self.root = root

    def tearDown(self):
        self.client.close()
        self.public.close()
        self.temp.cleanup()

    def test_trend_reads_are_protected_and_refresh_returns_completed_job(self):
        self.assertEqual(self.public.get("/api/trends").status_code, 401)
        before = self.client.get("/api/trends")
        self.assertEqual(before.status_code, 200)
        self.assertIsNone(before.json()["data"]["snapshot"])

        response = self.client.post("/api/operations/trend-refresh", json={
            "country_code": "ID", "date_range": "1DAY", "category_name": "ALL", "top_hashtag_limit": 30,
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["data"]["status"], "completed")
        after = self.client.get("/api/trends?category_name=ALL").json()["data"]
        self.assertEqual(len(after["hashtags"]), 30)
        self.assertEqual(len(after["videos"]), 600)
        self.assertEqual(after["hashtag_diagnostics"]["backend_returned"], 30)
        self.assertIn("ytdlp_available", after["configuration"])
        self.assertEqual(after["download_summary"]["targets"], 600)

    def test_download_requires_explicit_rights_confirmation(self):
        refresh = self.client.post("/api/operations/trend-refresh", json={})
        snapshot_id = refresh.json()["data"]["result"]["snapshot_id"]
        response = self.client.post("/api/operations/trend-download", json={
            "snapshot_id": snapshot_id,
            "rights_confirmed": False,
            "retry_failed": True,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Permission", response.json()["detail"])

    def test_oauth_start_is_protected_and_callback_never_returns_code_or_token(self):
        self.assertEqual(self.public.get("/api/integrations/tiktok/oauth/status").status_code, 401)
        started = self.client.post("/api/integrations/tiktok/oauth/start")
        self.assertEqual(started.status_code, 200)
        serialized = json.dumps(started.json())
        self.assertNotIn("app-secret", serialized)
        callback = self.public.get("/callback?auth_code=one-time-code&state=safe-state")
        self.assertEqual(callback.status_code, 200)
        self.assertEqual(self.oauth.callbacks, [("one-time-code", "safe-state")])
        self.assertNotIn("one-time-code", callback.text)
        self.assertNotIn("access_token", callback.text)
        self.assertEqual(callback.headers["cache-control"], "no-store")

    def test_media_link_rejects_escape_and_accepts_allowlisted_video(self):
        self.client.post("/api/operations/trend-refresh", json={})
        clip = self.root / "media" / "approved.mp4"
        clip.write_bytes(b"approved-video")
        escaped = self.client.put("/api/trends/videos/v1-1/media", json={"relative_path": "../outside.mp4"})
        self.assertEqual(escaped.status_code, 400)
        with mock.patch("clipper_app.application.trends._validate_video"):
            linked = self.client.put("/api/trends/videos/v1-1/media", json={"relative_path": "approved.mp4"})
        self.assertEqual(linked.status_code, 200)
        self.assertEqual(linked.json()["data"]["status"], "media_ready")


if __name__ == "__main__":
    unittest.main()
