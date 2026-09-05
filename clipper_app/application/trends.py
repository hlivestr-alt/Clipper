from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import urllib.parse
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import requests

from clipper_app.application.catalog import CatalogDatabase, utc_now
from clipper_app.application.hashtag_relevance import (
    MAX_RELEVANT_HASHTAGS,
    TIKTOK_TREND_SOURCE,
    HashtagRelevanceClassifier,
    filter_relevant_hashtags,
    normalize_hashtag_name,
)
from clipper_app.application.tiktok_oauth import (
    TikTokAuthorizationRequired,
    TikTokOAuthService,
)
from clipper_app.path_safety import UnsafePathError, resolve_within_root
from trend_analyzer import ANALYZER_VERSION, analyze_trend_video


SUPPORTED_TREND_MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
SUPPORTED_WINDOWS = {"1DAY", "7DAY", "30DAY", "120DAY"}
DEFAULT_TREND_HASHTAG_LIMIT = MAX_RELEVANT_HASHTAGS
MAX_TREND_HASHTAG_LIMIT = MAX_RELEVANT_HASHTAGS
DEFAULT_TREND_CATEGORY = "BEAUTY_AND_PERSONAL_CARE"
TIKTOK_TREND_CANDIDATE_MAX = 200
TIKTOK_VIDEO_HASHTAG_BATCH_LIMIT = 10
TIKTOK_RANKED_VIDEO_LIMIT = 20
TIKTOK_POST_METADATA_CONCURRENCY = 8
TIKTOK_PLAYER_DATA_SOURCE = "web_core"
TIKTOK_DOWNLOAD_CIRCUIT_BREAKER_THRESHOLD = 3
TIKTOK_MEDIA_HOST_SUFFIXES = (
    ".tiktok.com",
    ".tiktokcdn.com",
    ".tiktokv.com",
    ".byteoversea.com",
    ".ibytedtos.com",
)
SUGGESTED_PROFILE_FIELDS = {
    "hook_type",
    "subtitle_enabled",
    "subtitle_position",
    "subtitle_size",
    "subtitle_y_frac",
    "zoom_intensity",
    "letterbox_enabled",
    "letterbox_top_frac",
    "letterbox_bottom_frac",
    "color_grade",
}

logger = logging.getLogger(__name__)

_VIDEO_ACQUISITION_LOCKS_GUARD = threading.Lock()
_VIDEO_ACQUISITION_LOCKS: dict[tuple[str, str], threading.Lock] = {}

WINDOWS_INVALID_FOLDER_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
WINDOWS_RESERVED_FOLDER_NAMES = re.compile(
    r"^(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[1-9]|lpt[1-9])(?:\.|$)",
    re.IGNORECASE,
)


class TrendServiceError(RuntimeError):
    pass


class TikTokDiscoveryError(TrendServiceError):
    def __init__(self, message: str, *, code: Any = None, request_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True)
class DownloadFailure:
    category: str
    signature: str
    message: str
    systemic: bool


class DownloadCircuitBreaker:
    def __init__(self, threshold: int = TIKTOK_DOWNLOAD_CIRCUIT_BREAKER_THRESHOLD) -> None:
        self.threshold = max(2, int(threshold))
        self.signature = ""
        self.consecutive_count = 0
        self.failure: DownloadFailure | None = None

    @property
    def tripped(self) -> bool:
        return self.failure is not None

    def success(self) -> None:
        self.signature = ""
        self.consecutive_count = 0

    def failed(self, error: str) -> DownloadFailure:
        failure = _classify_download_error(error)
        if not failure.systemic:
            self.success()
            return failure
        if failure.signature == self.signature:
            self.consecutive_count += 1
        else:
            self.signature = failure.signature
            self.consecutive_count = 1
        if self.consecutive_count >= self.threshold:
            self.failure = failure
        return failure


class TikTokDiscoveryClient:
    base_url = "https://business-api.tiktok.com/open_api/v1.3"
    player_base_url = "https://www.tiktok.com"

    def __init__(
        self,
        access_token: str,
        advertiser_id: str,
        *,
        timeout: float = 30.0,
        token_provider: TikTokOAuthService | None = None,
    ) -> None:
        self._access_token = access_token
        self._advertiser_id = advertiser_id
        self.timeout = timeout
        self.token_provider = token_provider

    @property
    def advertiser_id(self) -> str:
        if self.token_provider is not None:
            return self.token_provider.credentials().advertiser_id
        return self._advertiser_id

    @classmethod
    def from_environment(cls) -> "TikTokDiscoveryClient":
        token = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
        advertiser = os.getenv("TIKTOK_ADVERTISER_ID", "").strip()
        if not token or not advertiser:
            raise TikTokDiscoveryError(
                "TikTok Discovery is not configured. Set TIKTOK_ACCESS_TOKEN and TIKTOK_ADVERTISER_ID."
            )
        return cls(token, advertiser)

    @classmethod
    def from_oauth_service(cls, service: TikTokOAuthService) -> "TikTokDiscoveryClient":
        return cls("", "", token_provider=service)

    def trending_hashtags(self, *, country_code: str, date_range: str, category_name: str) -> dict[str, Any]:
        logger.info(
            "TikTok hashtag discovery request: requested_count=%d request_count_parameter=unsupported "
            "pagination=unsupported country=%s date_range=%s category=%s",
            TIKTOK_TREND_CANDIDATE_MAX,
            country_code,
            date_range,
            category_name,
        )
        payload = self._get("/discovery/trending_list/", {
            "advertiser_id": self.advertiser_id,
            "discovery_type": "HASHTAG",
            "country_code": country_code,
            "date_range": date_range,
            "category_name": category_name,
        })
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        pagination = {
            key: data[key]
            for key in ("page_info", "pagination", "cursor", "next_cursor", "has_more")
            if key in data
        }
        logger.info(
            "TikTok hashtag discovery response: requested_count=%d returned_count=%d "
            "pagination=%s filter_info=%s request_id=%s",
            TIKTOK_TREND_CANDIDATE_MAX,
            len(_payload_list(payload)),
            pagination or "not_provided",
            data.get("filter_info"),
            payload.get("request_id"),
        )
        return payload

    def trending_videos(self, hashtag_ids: list[str], *, country_code: str, date_range: str) -> dict[str, Any]:
        if not hashtag_ids or len(hashtag_ids) > TIKTOK_VIDEO_HASHTAG_BATCH_LIMIT:
            raise TikTokDiscoveryError(
                f"TikTok video discovery accepts between 1 and {TIKTOK_VIDEO_HASHTAG_BATCH_LIMIT} hashtag IDs per request."
            )
        payload = self._get("/discovery/video_list/", {
            "advertiser_id": self.advertiser_id,
            "discovery_type": "HASHTAG",
            "hashtag_ids": json.dumps(hashtag_ids, separators=(",", ":")),
            "country_code": country_code,
            "date_range": date_range,
        })
        groups = _payload_list(payload)
        candidate_counts = {
            str(group.get("hashtag_id") or ""): len(group.get("top_video_list") or [])
            for group in groups
        }
        logger.info(
            "TikTok ranked video response: endpoint=/discovery/video_list/ hashtags=%d "
            "candidate_counts=%s candidate_limit_per_hashtag=%d pagination=unsupported request_id=%s",
            len(hashtag_ids),
            candidate_counts,
            TIKTOK_RANKED_VIDEO_LIMIT,
            payload.get("request_id"),
        )
        return payload

    def post_metadata(self, video_id: str) -> dict[str, Any]:
        """Fetch the provider player record that distinguishes image and video posts.

        The Business Discovery response exposes only IDs and URLs. TikTok's own
        player requests this record to choose its image-post or video renderer.
        This endpoint is provider-owned but not part of the documented Business
        API contract, so failures are handled per candidate and default to exclude.
        """
        normalized_id = str(video_id or "").strip()
        if not normalized_id.isdigit():
            raise TikTokDiscoveryError("TikTok player metadata requires a numeric video ID.")
        url = f"{self.player_base_url}/player/api/v1/items"
        try:
            response = requests.get(
                url,
                params={
                    "item_ids": normalized_id,
                    "language": "en-US",
                    "aid": "1459",
                    "data_source": TIKTOK_PLAYER_DATA_SOURCE,
                },
                headers={
                    "Accept": "application/json",
                    "Referer": f"{self.player_base_url}/player/v1/{normalized_id}",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=min(self.timeout, 12.0),
            )
        except requests.RequestException as exc:
            raise TikTokDiscoveryError(_discovery_transport_error(exc, min(self.timeout, 12.0))) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise TikTokDiscoveryError("TikTok player metadata returned invalid JSON.") from exc
        if response.status_code >= 400 or not isinstance(payload, dict):
            raise TikTokDiscoveryError(f"TikTok player metadata HTTP {response.status_code}.")
        return payload

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        access_token = self._access_token
        if self.token_provider is not None:
            try:
                access_token = self.token_provider.credentials().access_token
            except TikTokAuthorizationRequired as exc:
                raise TikTokDiscoveryError(str(exc)) from exc
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(
                url,
                params=params,
                headers={"Access-Token": access_token, "Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TikTokDiscoveryError(_discovery_transport_error(exc, self.timeout)) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise TikTokDiscoveryError("TikTok Discovery returned an invalid JSON response.") from exc
        if not isinstance(payload, dict):
            raise TikTokDiscoveryError("TikTok Discovery returned an invalid response payload.")
        if response.status_code >= 400:
            if (response.status_code == 401 or _provider_auth_rejected(payload)) and self.token_provider is not None:
                self.token_provider.mark_authorization_invalid(
                    str(payload.get("message") or "TikTok rejected the stored access token.")
                )
            raise TikTokDiscoveryError(
                _sanitize_provider_message(
                    str(payload.get("message") or f"TikTok Discovery HTTP {response.status_code}"),
                    access_token,
                ),
                code=payload.get("code"),
                request_id=str(payload.get("request_id") or ""),
            )
        if _provider_auth_rejected(payload):
            if self.token_provider is not None:
                self.token_provider.mark_authorization_invalid(str(payload.get("message") or "TikTok rejected the token."))
            raise TikTokDiscoveryError("TikTok authorization was revoked or rejected. Reauthorization is required.")
        if payload.get("code") not in {0, "0", None}:
            raise TikTokDiscoveryError(
                _sanitize_provider_message(
                    str(payload.get("message") or "TikTok Discovery rejected the request."),
                    access_token,
                ),
                code=payload.get("code"),
                request_id=str(payload.get("request_id") or ""),
            )
        return payload


class TrendRepository:
    def __init__(self, database: CatalogDatabase) -> None:
        self.database = database

    def save_snapshot(
        self,
        *,
        country_code: str,
        date_range: str,
        category_name: str,
        hashtag_payload: dict[str, Any],
        video_payload: dict[str, Any],
        hashtag_diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot_id = uuid4().hex
        retrieved_at = utc_now()
        hashtags = _payload_list(hashtag_payload)
        hashtag_names_by_id = {
            str(item.get("hashtag_id") or ""): str(item.get("hashtag_name") or "")
            for item in hashtags
        }
        video_groups = _payload_list(video_payload)
        classified_groups: list[
            tuple[dict[str, Any], list[tuple[int, int | None, dict[str, Any], dict[str, Any]]]]
        ] = []
        video_diagnostics: list[dict[str, Any]] = []
        for group in video_groups:
            classified_videos: list[tuple[int, int | None, dict[str, Any], dict[str, Any]]] = []
            ranked_video_ids: set[str] = set()
            final_rank = 0
            diagnostics = {
                "hashtag_id": str(group.get("hashtag_id") or ""),
                "hashtag_name": str(group.get("hashtag_name") or ""),
                "total_candidates_returned": 0,
                "video_posts_detected": 0,
                "image_carousel_posts_excluded": 0,
                "unknown_posts_excluded": 0,
                "unavailable_posts_excluded": 0,
                "valid_videos_stored": 0,
                "pagination_available": False,
                "candidate_limit": TIKTOK_RANKED_VIDEO_LIMIT,
            }
            for ordinal, video in enumerate(group.get("top_video_list") or [], start=1):
                if not isinstance(video, dict):
                    continue
                classification = _classify_tiktok_post(video)
                video_id = str(video.get("video_id") or "")
                assigned_rank = None
                if (
                    classification["media_type"] == "video"
                    and classification["is_available"]
                    and video_id
                    and video_id not in ranked_video_ids
                    and final_rank < TIKTOK_RANKED_VIDEO_LIMIT
                ):
                    final_rank += 1
                    assigned_rank = final_rank
                    ranked_video_ids.add(video_id)
                classified_videos.append((ordinal, assigned_rank, video, classification))
                diagnostics["total_candidates_returned"] += 1
                if classification["media_type"] == "video":
                    diagnostics["video_posts_detected"] += 1
                if classification["exclusion_reason"] == "image_or_carousel":
                    diagnostics["image_carousel_posts_excluded"] += 1
                elif classification["exclusion_reason"] == "unknown":
                    diagnostics["unknown_posts_excluded"] += 1
                elif classification["exclusion_reason"] == "unavailable":
                    diagnostics["unavailable_posts_excluded"] += 1
                elif assigned_rank is not None:
                    diagnostics["valid_videos_stored"] += 1
            classified_groups.append((group, classified_videos))
            video_diagnostics.append(diagnostics)
        request_ids = [str(hashtag_payload.get("request_id") or "")]
        request_ids.extend(str(value) for value in (video_payload.get("request_ids") or []) if value)
        if video_payload.get("request_id"):
            request_ids.append(str(video_payload["request_id"]))
        request_ids = list(dict.fromkeys(value for value in request_ids if value))
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO trend_snapshots(snapshot_id,retrieved_at,country_code,date_range,category_name,provider_request_id,payload_json) VALUES(?,?,?,?,?,?,?)",
                (
                    snapshot_id, retrieved_at, country_code, date_range, category_name,
                    ",".join(request_ids),
                    json.dumps({
                        "hashtag_count": len(hashtags),
                        "video_group_count": len(video_groups),
                        "provider_request_ids": request_ids,
                        "video_endpoint": "/open_api/v1.3/discovery/video_list/",
                        "video_candidate_limit_per_hashtag": TIKTOK_RANKED_VIDEO_LIMIT,
                        "video_pagination_available": False,
                        "video_diagnostics": video_diagnostics,
                        "hashtag_filter": hashtag_diagnostics or {},
                    }, separators=(",", ":")),
                ),
            )
            for fallback_display_rank, item in enumerate(hashtags, start=1):
                original_rank = int(item.get("original_rank") or item.get("rank_position") or 0)
                display_rank = int(item.get("display_rank") or fallback_display_rank)
                connection.execute(
                    "INSERT INTO trend_hashtags("
                    "snapshot_id,hashtag_id,hashtag_name,normalized_name,source,source_category,"
                    "original_rank,display_rank,relevance_type,matched_brand,classification_reason,"
                    "rank_position,rank_change,views,posts,payload_json"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id, str(item.get("hashtag_id") or ""), str(item.get("hashtag_name") or ""),
                        str(item.get("normalized_name") or normalize_hashtag_name(item.get("hashtag_name"))),
                        str(item.get("source") or TIKTOK_TREND_SOURCE),
                        str(item.get("source_category") or category_name),
                        original_rank,
                        display_rank,
                        str(item.get("relevance_type") or ""),
                        str(item.get("matched_brand") or "") or None,
                        str(item.get("classification_reason") or ""),
                        original_rank, str(item.get("rank_change") or ""),
                        _optional_int(item.get("views")), _optional_int(item.get("posts")),
                        json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            for group, classified_videos in classified_groups:
                hashtag_id = str(group.get("hashtag_id") or "")
                hashtag_name = hashtag_names_by_id.get(
                    hashtag_id, str(group.get("hashtag_name") or "")
                )
                for ordinal, final_rank, video, classification in classified_videos:
                    video_id = str(video.get("video_id") or "")
                    if not hashtag_id or not video_id:
                        continue
                    persisted_payload = {
                        key: value
                        for key, value in video.items()
                        if key != "_post_metadata"
                    }
                    persisted_payload["_classification"] = {
                        "media_type": classification["media_type"],
                        "is_available": classification["is_available"],
                        "classification_evidence": classification["classification_evidence"],
                        "availability_evidence": classification["availability_evidence"],
                    }
                    connection.execute(
                        "INSERT OR IGNORE INTO trend_videos("
                        "snapshot_id,hashtag_id,video_id,hashtag_name,provider_ordinal,final_rank,share_url,embed_url,"
                        "media_type,is_available,classification_evidence,availability_evidence,"
                        "video_duration_seconds,image_count,playable_url_count,provider_aweme_type,"
                        "exclusion_reason,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            snapshot_id, hashtag_id, video_id, hashtag_name, ordinal, final_rank,
                            str(video.get("share_url") or ""), str(video.get("embed_url") or ""),
                            classification["media_type"], int(classification["is_available"]),
                            classification["classification_evidence"],
                            classification["availability_evidence"],
                            classification["video_duration_seconds"], classification["image_count"],
                            classification["playable_url_count"], classification["provider_aweme_type"],
                            classification["exclusion_reason"],
                            json.dumps(persisted_payload, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
        candidate_count = sum(item["total_candidates_returned"] for item in video_diagnostics)
        valid_video_count = sum(item["valid_videos_stored"] for item in video_diagnostics)
        return {
            "snapshot_id": snapshot_id,
            "retrieved_at": retrieved_at,
            "hashtag_count": len(hashtags),
            "candidate_count": candidate_count,
            "video_count": valid_video_count,
            "valid_video_count": valid_video_count,
            "video_batch_count": int(video_payload.get("batch_count") or 0),
            "video_diagnostics": video_diagnostics,
        }

    def latest_snapshot(self, country_code: str, date_range: str, category_name: str) -> dict[str, Any] | None:
        rows = self.database.query(
            "SELECT * FROM trend_snapshots WHERE country_code=? AND date_range=? AND category_name=? ORDER BY retrieved_at DESC LIMIT 1",
            (country_code, date_range, category_name),
        )
        return dict(rows[0]) if rows else None

    def snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        rows = self.database.query("SELECT * FROM trend_snapshots WHERE snapshot_id=?", (snapshot_id,))
        return dict(rows[0]) if rows else None

    def hashtags(self, snapshot_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.database.query(
            "SELECT hashtag_id,hashtag_name,normalized_name,source,source_category,original_rank,"
            "display_rank,relevance_type,matched_brand,classification_reason,rank_position,"
            "rank_change,views,posts FROM trend_hashtags WHERE snapshot_id=? "
            "ORDER BY CASE WHEN display_rank>0 THEN display_rank ELSE rank_position END",
            (snapshot_id,),
        )]

    def candidates(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT v.*,h.rank_position,m.relative_path,m.file_sha256,m.status AS media_status,m.error AS media_error,"
            "d.status AS download_status,d.error AS download_error,d.relative_path AS downloaded_relative_path,"
            "d.snapshot_id AS download_snapshot_id "
            "FROM trend_videos v JOIN trend_hashtags h ON h.snapshot_id=v.snapshot_id AND h.hashtag_id=v.hashtag_id "
            "LEFT JOIN trend_media_links m ON m.video_id=v.video_id "
            "LEFT JOIN trend_media_downloads d ON d.snapshot_id=v.snapshot_id "
            "AND d.hashtag_id=v.hashtag_id AND d.video_id=v.video_id WHERE v.snapshot_id=? "
            "ORDER BY h.rank_position,v.provider_ordinal",
            (snapshot_id,),
        )
        return [dict(row) for row in rows]

    def videos(self, snapshot_id: str) -> list[dict[str, Any]]:
        return [
            row for row in self.candidates(snapshot_id)
            if (
                row.get("media_type") == "video"
                and bool(row.get("is_available"))
                and 1 <= int(row.get("final_rank") or 0) <= TIKTOK_RANKED_VIDEO_LIMIT
            )
        ]

    def unique_videos(self, snapshot_id: str) -> list[dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in self.videos(snapshot_id):
            result.setdefault(str(row["video_id"]), row)
        return list(result.values())

    def download(self, snapshot_id: str, hashtag_id: str, video_id: str) -> dict[str, Any] | None:
        rows = self.database.query(
            "SELECT * FROM trend_media_downloads WHERE snapshot_id=? AND hashtag_id=? AND video_id=?",
            (snapshot_id, hashtag_id, video_id),
        )
        return dict(rows[0]) if rows else None

    def queue_download(
        self,
        *,
        video_id: str,
        snapshot_id: str,
        hashtag_id: str,
        hashtag_name: str,
        normalized_hashtag: str,
        final_rank: int,
        run_id: str,
        source_url: str,
    ) -> None:
        now = utc_now()
        self.database.execute(
            "INSERT INTO trend_media_downloads("
            "snapshot_id,hashtag_id,video_id,hashtag_name,normalized_hashtag,final_rank,run_id,source_url,"
            "status,attempt_count,updated_at) VALUES(?,?,?,?,?,?,?,?,'queued',1,?) "
            "ON CONFLICT DO UPDATE SET "
            "hashtag_name=excluded.hashtag_name,normalized_hashtag=excluded.normalized_hashtag,"
            "final_rank=excluded.final_rank,run_id=excluded.run_id,source_url=excluded.source_url,status='queued',"
            "error=NULL,attempt_count=trend_media_downloads.attempt_count+1,started_at=NULL,completed_at=NULL,updated_at=excluded.updated_at",
            (
                snapshot_id, hashtag_id, video_id, hashtag_name, normalized_hashtag, final_rank,
                run_id, source_url, now,
            ),
        )

    def start_download(self, snapshot_id: str, hashtag_id: str, video_id: str) -> None:
        now = utc_now()
        self.database.execute(
            "UPDATE trend_media_downloads SET status='downloading',error=NULL,started_at=?,updated_at=? "
            "WHERE snapshot_id=? AND hashtag_id=? AND video_id=?",
            (now, now, snapshot_id, hashtag_id, video_id),
        )

    def complete_download(
        self,
        snapshot_id: str,
        hashtag_id: str,
        video_id: str,
        *,
        relative_path: str,
        file_sha256: str,
        stat: Any,
        duration_seconds: float,
        extractor_version: str,
    ) -> None:
        now = utc_now()
        self.database.execute(
            "UPDATE trend_media_downloads SET status='downloaded',relative_path=?,file_sha256=?,file_size=?,file_mtime_ns=?,"
            "duration_seconds=?,error=NULL,extractor_version=?,completed_at=?,updated_at=? "
            "WHERE snapshot_id=? AND hashtag_id=? AND video_id=?",
            (
                relative_path, file_sha256, int(stat.st_size), int(stat.st_mtime_ns), float(duration_seconds),
                extractor_version, now, now, snapshot_id, hashtag_id, video_id,
            ),
        )

    def fail_download(self, snapshot_id: str, hashtag_id: str, video_id: str, error: str) -> None:
        now = utc_now()
        self.database.execute(
            "UPDATE trend_media_downloads SET status='failed',error=?,completed_at=?,updated_at=? "
            "WHERE snapshot_id=? AND hashtag_id=? AND video_id=?",
            (_sanitize_download_error(error), now, now, snapshot_id, hashtag_id, video_id),
        )

    def record_failed_reference(
        self,
        *,
        row: dict[str, Any],
        run_id: str,
        source_url: str,
        error: str,
        attempted: bool,
    ) -> None:
        now = utc_now()
        self.database.execute(
            "INSERT INTO trend_media_downloads("
            "snapshot_id,hashtag_id,video_id,hashtag_name,normalized_hashtag,final_rank,run_id,source_url,"
            "status,error,attempt_count,started_at,completed_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,'failed',?,?,?, ?,?) "
            "ON CONFLICT(snapshot_id,hashtag_id,video_id) DO UPDATE SET "
            "hashtag_name=excluded.hashtag_name,normalized_hashtag=excluded.normalized_hashtag,"
            "final_rank=excluded.final_rank,run_id=excluded.run_id,source_url=excluded.source_url,"
            "status='failed',error=excluded.error,"
            "attempt_count=trend_media_downloads.attempt_count+excluded.attempt_count,"
            "started_at=excluded.started_at,completed_at=excluded.completed_at,updated_at=excluded.updated_at",
            (
                str(row["snapshot_id"]), str(row["hashtag_id"]), str(row["video_id"]),
                str(row.get("hashtag_name") or ""),
                normalize_hashtag_folder_name(row.get("hashtag_name")), int(row["final_rank"]),
                run_id, source_url, _sanitize_download_error(error), 1 if attempted else 0,
                now if attempted else None, now, now,
            ),
        )

    def interrupt_download(
        self,
        *,
        row: dict[str, Any],
        run_id: str,
        source_url: str,
        error: str,
    ) -> None:
        """Record an unattempted item without increasing its attempt count."""
        now = utc_now()
        self.database.execute(
            "INSERT INTO trend_media_downloads("
            "snapshot_id,hashtag_id,video_id,hashtag_name,normalized_hashtag,final_rank,run_id,source_url,"
            "status,error,attempt_count,completed_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,'interrupted',?,0,?,?) "
            "ON CONFLICT DO UPDATE SET "
            "hashtag_name=excluded.hashtag_name,normalized_hashtag=excluded.normalized_hashtag,"
            "final_rank=excluded.final_rank,run_id=excluded.run_id,source_url=excluded.source_url,"
            "status='interrupted',error=excluded.error,completed_at=excluded.completed_at,"
            "updated_at=excluded.updated_at",
            (
                str(row["snapshot_id"]), str(row["hashtag_id"]), str(row["video_id"]),
                str(row.get("hashtag_name") or ""),
                normalize_hashtag_folder_name(row.get("hashtag_name")),
                int(row["final_rank"]), run_id, source_url,
                _sanitize_download_error(error), now, now,
            ),
        )

    def complete_hashtag_downloads(
        self,
        records: Iterable[dict[str, Any]],
        *,
        ranked_rows: Iterable[dict[str, Any]],
        failed_errors: dict[str, str],
        extractor_version: str,
        actor: str,
    ) -> None:
        now = utc_now()
        records = list(records)
        ranked_rows = list(ranked_rows)
        with self.database.transaction(immediate=True) as connection:
            if ranked_rows:
                snapshot_id = str(ranked_rows[0]["snapshot_id"])
                hashtag_id = str(ranked_rows[0]["hashtag_id"])
                for video_id, error in failed_errors.items():
                    connection.execute(
                        "UPDATE trend_media_downloads SET status='failed',error=?,completed_at=?,updated_at=? "
                        "WHERE snapshot_id=? AND hashtag_id=? AND video_id=?",
                        (
                            _sanitize_download_error(error), now, now,
                            snapshot_id, hashtag_id, video_id,
                        ),
                    )
            for record in records:
                connection.execute(
                    "UPDATE trend_media_downloads SET status='downloaded',relative_path=?,file_sha256=?,"
                    "file_size=?,file_mtime_ns=?,duration_seconds=?,final_rank=?,error=NULL,extractor_version=?,"
                    "completed_at=?,updated_at=? WHERE snapshot_id=? AND hashtag_id=? AND video_id=?",
                    (
                        record["relative_path"], record["file_sha256"], record["file_size"],
                        record["file_mtime_ns"], record["duration_seconds"], record["final_rank"],
                        extractor_version,
                        now, now, record["snapshot_id"], record["hashtag_id"], record["video_id"],
                    ),
                )
                connection.execute(
                    "INSERT INTO trend_media_links("
                    "video_id,relative_path,file_sha256,file_size,file_mtime_ns,status,approved_at,"
                    "approved_by,error,updated_at) VALUES(?,?,?,?,?,'media_ready',?,?,NULL,?) "
                    "ON CONFLICT(video_id) DO UPDATE SET relative_path=excluded.relative_path,"
                    "file_sha256=excluded.file_sha256,file_size=excluded.file_size,"
                    "file_mtime_ns=excluded.file_mtime_ns,status='media_ready',"
                    "approved_at=excluded.approved_at,approved_by=excluded.approved_by,"
                    "error=NULL,updated_at=excluded.updated_at",
                    (
                        record["video_id"], record["relative_path"], record["file_sha256"],
                        record["file_size"], record["file_mtime_ns"], now, actor, now,
                    ),
                )

    def reconcile_interrupted_downloads(self) -> int:
        now = utc_now()
        count = int(self.database.scalar(
            "SELECT COUNT(*) FROM trend_media_downloads WHERE status IN ('queued','downloading')", default=0
        ) or 0)
        self.database.execute(
            "UPDATE trend_media_downloads SET status='interrupted',error='Download interrupted; run Save all videos to resume.',"
            "completed_at=?,updated_at=? WHERE status IN ('queued','downloading')",
            (now, now),
        )
        return count

    def download_summary(
        self,
        snapshot_id: str,
        *,
        video_rows: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        rows = list(video_rows) if video_rows is not None else self.videos(snapshot_id)
        unique_rows = {str(row["video_id"]): row for row in rows}
        saved_ids = {
            video_id for video_id, row in unique_rows.items()
            if row.get("media_status") in {"media_ready", "analyzing", "analyzed"}
            and bool(row.get("relative_path"))
        }
        summary = {
            "targets": len(rows),
            "target_references": len(rows),
            "unique_videos": len(unique_rows),
            "saved": len(saved_ids),
            "new": len(unique_rows) - len(saved_ids),
            "queued": 0, "downloading": 0, "downloaded": 0,
            "reused": 0, "approved": len(saved_ids), "failed": 0, "interrupted": 0,
        }
        for row in rows:
            status = str(row.get("download_status") or "")
            if status in summary:
                summary[status] += 1
        return summary

    def record_reference(
        self,
        *,
        row: dict[str, Any],
        run_id: str,
        source_url: str,
        status: str,
        media: dict[str, Any],
        extractor_version: str,
        attempted: bool,
    ) -> None:
        now = utc_now()
        self.database.execute(
            "INSERT INTO trend_media_downloads("
            "snapshot_id,hashtag_id,video_id,hashtag_name,normalized_hashtag,final_rank,run_id,source_url,"
            "relative_path,file_sha256,file_size,file_mtime_ns,duration_seconds,status,error,"
            "extractor_version,attempt_count,started_at,completed_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?) "
            "ON CONFLICT(snapshot_id,hashtag_id,video_id) DO UPDATE SET "
            "hashtag_name=excluded.hashtag_name,normalized_hashtag=excluded.normalized_hashtag,"
            "final_rank=excluded.final_rank,run_id=excluded.run_id,source_url=excluded.source_url,"
            "relative_path=excluded.relative_path,file_sha256=excluded.file_sha256,"
            "file_size=excluded.file_size,file_mtime_ns=excluded.file_mtime_ns,"
            "duration_seconds=excluded.duration_seconds,status=excluded.status,error=NULL,"
            "extractor_version=excluded.extractor_version,"
            "attempt_count=trend_media_downloads.attempt_count+excluded.attempt_count,"
            "started_at=excluded.started_at,completed_at=excluded.completed_at,updated_at=excluded.updated_at",
            (
                str(row["snapshot_id"]), str(row["hashtag_id"]), str(row["video_id"]),
                str(row.get("hashtag_name") or ""),
                normalize_hashtag_folder_name(row.get("hashtag_name")), int(row["final_rank"]),
                run_id, source_url, str(media["relative_path"]), str(media["file_sha256"]),
                int(media["file_size"]), int(media["file_mtime_ns"]),
                float(media.get("duration_seconds") or 0.0), status, extractor_version,
                1 if attempted else 0, now if attempted else None, now, now,
            ),
        )

    def link_media(self, video_id: str, relative_path: str, file_sha256: str, stat: Any, actor: str) -> dict[str, Any]:
        now = utc_now()
        self.database.execute(
            "INSERT INTO trend_media_links(video_id,relative_path,file_sha256,file_size,file_mtime_ns,status,approved_at,approved_by,error,validated_at,updated_at) "
            "VALUES(?,?,?,?,?,'media_ready',?,?,NULL,?,?) ON CONFLICT(video_id) DO UPDATE SET "
            "relative_path=excluded.relative_path,file_sha256=excluded.file_sha256,file_size=excluded.file_size,file_mtime_ns=excluded.file_mtime_ns," 
            "status='media_ready',approved_at=excluded.approved_at,approved_by=excluded.approved_by,error=NULL,"
            "validated_at=excluded.validated_at,updated_at=excluded.updated_at",
            (video_id, relative_path, file_sha256, int(stat.st_size), int(stat.st_mtime_ns), now, actor, now, now),
        )
        return {"video_id": video_id, "relative_path": relative_path, "file_sha256": file_sha256, "status": "media_ready", "approved_at": now, "approved_by": actor}

    def media_link(self, video_id: str) -> dict[str, Any] | None:
        rows = self.database.query("SELECT * FROM trend_media_links WHERE video_id=?", (video_id,))
        return dict(rows[0]) if rows else None

    def set_media_status(self, video_id: str, status: str, error: str | None = None) -> None:
        self.database.execute(
            "UPDATE trend_media_links SET status=?,error=?,updated_at=? WHERE video_id=?",
            (status, error[:1000] if error else None, utc_now(), video_id),
        )

    def cached_fingerprint(self, snapshot_id: str, video_id: str, file_sha256: str) -> dict[str, Any] | None:
        rows = self.database.query(
            "SELECT * FROM trend_fingerprints WHERE snapshot_id=? AND video_id=? AND file_sha256=? AND analyzer_version=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
            (snapshot_id, video_id, file_sha256, ANALYZER_VERSION),
        )
        if not rows:
            return None
        return json.loads(rows[0]["payload_json"])

    def save_fingerprint(self, snapshot_id: str, video_id: str, hashtag_id: str, file_sha256: str, status: str, payload: dict[str, Any]) -> dict[str, Any]:
        fingerprint_id = str(payload.get("fingerprint_id") or uuid4().hex)
        payload = {**payload, "fingerprint_id": fingerprint_id, "snapshot_id": snapshot_id, "hashtag_id": hashtag_id}
        self.database.execute(
            "INSERT INTO trend_fingerprints(fingerprint_id,snapshot_id,video_id,hashtag_id,file_sha256,analyzer_version,status,created_at,payload_json) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_id,video_id,file_sha256,analyzer_version) DO UPDATE SET "
            "fingerprint_id=excluded.fingerprint_id,hashtag_id=excluded.hashtag_id,status=excluded.status,created_at=excluded.created_at,payload_json=excluded.payload_json",
            (fingerprint_id, snapshot_id, video_id, hashtag_id, file_sha256, ANALYZER_VERSION, status, utc_now(), json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )
        return payload

    def save_pattern(self, snapshot_id: str, base_revision: str, payload: dict[str, Any]) -> dict[str, Any]:
        pattern_id = str(payload.get("pattern_id") or uuid4().hex)
        created_at = utc_now()
        payload = {**payload, "pattern_id": pattern_id, "snapshot_id": snapshot_id, "created_at": created_at}
        self.database.execute(
            "INSERT INTO trend_patterns(pattern_id,snapshot_id,created_at,base_profile_revision,payload_json) VALUES(?,?,?,?,?)",
            (pattern_id, snapshot_id, created_at, base_revision, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )
        return payload

    def latest_pattern(self, snapshot_id: str) -> dict[str, Any] | None:
        rows = self.database.query("SELECT payload_json FROM trend_patterns WHERE snapshot_id=? ORDER BY created_at DESC LIMIT 1", (snapshot_id,))
        return json.loads(rows[0]["payload_json"]) if rows else None

    def pattern(self, pattern_id: str) -> dict[str, Any] | None:
        rows = self.database.query("SELECT payload_json FROM trend_patterns WHERE pattern_id=?", (pattern_id,))
        return json.loads(rows[0]["payload_json"]) if rows else None


class TrendService:
    def __init__(
        self,
        database: CatalogDatabase,
        cfg: Any,
        *,
        client_factory: Any = None,
        download_runner: Any = None,
        oauth_service: TikTokOAuthService | None = None,
        hashtag_classifier: HashtagRelevanceClassifier | None = None,
    ) -> None:
        self.database = database
        self.cfg = cfg
        self.repository = TrendRepository(database)
        self.oauth_service = oauth_service
        if client_factory is None:
            self.oauth_service = oauth_service or TikTokOAuthService.from_environment(cfg)
            oauth_client_factory = TikTokDiscoveryClient.from_oauth_service
            self.client_factory = lambda: oauth_client_factory(self.oauth_service)
        else:
            self.client_factory = client_factory
        self.download_runner = download_runner or _run_ytdlp
        self.hashtag_classifier = hashtag_classifier or HashtagRelevanceClassifier()
        self._ytdlp_cache: tuple[bool, str] | None = None
        self.repository.reconcile_interrupted_downloads()

    @property
    def media_root(self) -> Path:
        configured = Path(str(getattr(self.cfg, "TREND_MEDIA_DIR", r"D:\Trend Videos") or r"D:\Trend Videos"))
        return (configured if configured.is_absolute() else Path.cwd() / configured).resolve()

    @property
    def legacy_media_root(self) -> Path:
        configured = Path(str(
            getattr(self.cfg, "TREND_LEGACY_MEDIA_DIR", "working/trends/media")
            or "working/trends/media"
        ))
        return (configured if configured.is_absolute() else Path.cwd() / configured).resolve()

    def configuration(self) -> dict[str, Any]:
        root = self.media_root
        ytdlp_available, ytdlp_version = self._ytdlp_configuration()
        free_bytes = _free_disk_bytes(root)
        reserve = max(0, int(getattr(self.cfg, "TREND_YTDLP_MIN_FREE_BYTES", 5 * 1024**3)))
        oauth_status = self.oauth_service.status() if self.oauth_service is not None else {}
        return {
            "app_configured": bool(oauth_status.get("app_configured")) if oauth_status else bool(os.getenv("TIKTOK_APP_ID", "").strip() and os.getenv("TIKTOK_APP_SECRET", "").strip()),
            "access_configured": bool(oauth_status.get("connected") and oauth_status.get("selected_advertiser_id")) if oauth_status else bool(os.getenv("TIKTOK_ACCESS_TOKEN", "").strip() and os.getenv("TIKTOK_ADVERTISER_ID", "").strip()),
            "oauth": oauth_status,
            "media_dir": str(root),
            "media_dir_exists": root.is_dir(),
            "qwen_enabled": bool(getattr(self.cfg, "TREND_QWEN_ENABLED", False)),
            "ytdlp_available": ytdlp_available,
            "ytdlp_version": ytdlp_version,
            "download_concurrency": max(1, min(4, int(getattr(self.cfg, "TREND_YTDLP_CONCURRENCY", 2)))),
            "download_timeout_seconds": max(30, int(getattr(self.cfg, "TREND_YTDLP_TIMEOUT_SECONDS", 600))),
            "download_min_free_bytes": reserve,
            "media_free_bytes": free_bytes,
            "disk_reserve_satisfied": free_bytes >= reserve,
        }

    def _ytdlp_configuration(self) -> tuple[bool, str]:
        if self._ytdlp_cache is not None:
            return self._ytdlp_cache
        try:
            result = subprocess.run(
                [sys.executable, "-m", "yt_dlp", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            version = (result.stdout or "").strip().splitlines()[0][:80] if result.returncode == 0 else ""
            self._ytdlp_cache = (bool(version), version)
        except (OSError, subprocess.SubprocessError):
            self._ytdlp_cache = (False, "")
        return self._ytdlp_cache

    def refresh(self, request: Any) -> dict[str, Any]:
        country = _country_code(request.country_code)
        window = str(request.date_range or "1DAY").upper()
        if window not in SUPPORTED_WINDOWS:
            raise TrendServiceError(f"Unsupported TikTok trend window: {window}")
        category = str(request.category_name or DEFAULT_TREND_CATEGORY).upper()
        limit = max(1, min(MAX_TREND_HASHTAG_LIMIT, int(request.top_hashtag_limit or DEFAULT_TREND_HASHTAG_LIMIT)))
        client = self.client_factory()
        hashtag_payload = client.trending_hashtags(country_code=country, date_range=window, category_name=category)
        filter_result = filter_relevant_hashtags(
            _payload_list(hashtag_payload),
            limit=limit,
            classifier=self.hashtag_classifier,
            default_source=TIKTOK_TREND_SOURCE,
            default_source_category=category,
        )
        hashtag_diagnostics = {
            "requested": TIKTOK_TREND_CANDIDATE_MAX,
            "request_count_parameter_supported": False,
            "source": TIKTOK_TREND_SOURCE,
            "source_category": category,
            "total_candidates_returned": filter_result.total_count,
            "accepted_topical": filter_result.topical_count,
            "accepted_brands": filter_result.brand_count,
            "classified_relevant": filter_result.relevant_count,
            "excluded": filter_result.excluded_count,
            "deduplicated": filter_result.deduplicated_count,
            "stored": len(filter_result.selected),
            "backend_returned": len(filter_result.selected),
            "selection_limit": limit,
            "pagination": _payload_pagination(hashtag_payload),
            "classifications": [
                {
                    "hashtag": item.hashtag,
                    "normalized_name": item.normalized_name,
                    "source": item.source,
                    "source_category": item.source_category,
                    "original_rank": item.original_rank,
                    "display_rank": item.display_rank,
                    "relevant": item.relevant,
                    "relevance_type": item.relevance_type,
                    "category": item.relevance_type,
                    "classification_reason": item.reason,
                    "reason": item.reason,
                    "matched_brand": item.matched_brand,
                }
                for item in filter_result.classifications
            ],
            "exclusions": [
                {
                    "hashtag": item.hashtag,
                    "normalized_name": item.normalized_name,
                    "source": item.source,
                    "source_category": item.source_category,
                    "original_rank": item.original_rank,
                    "relevance_type": item.relevance_type,
                    "category": item.relevance_type,
                    "classification_reason": item.reason,
                    "reason": item.reason,
                }
                for item in filter_result.exclusions
            ],
        }
        filtered_hashtag_payload = _replace_payload_list(hashtag_payload, filter_result.selected)
        ids = [str(item.get("hashtag_id")) for item in filter_result.selected if item.get("hashtag_id")]
        video_groups: list[dict[str, Any]] = []
        video_request_ids: list[str] = []
        for start in range(0, len(ids), TIKTOK_VIDEO_HASHTAG_BATCH_LIMIT):
            batch = ids[start:start + TIKTOK_VIDEO_HASHTAG_BATCH_LIMIT]
            payload = client.trending_videos(batch, country_code=country, date_range=window)
            video_groups.extend(_payload_list(payload))
            if payload.get("request_id"):
                video_request_ids.append(str(payload["request_id"]))
        _enrich_ranked_video_groups(
            client,
            video_groups,
            concurrency=max(
                1,
                min(
                    16,
                    int(getattr(self.cfg, "TREND_TIKTOK_METADATA_CONCURRENCY", TIKTOK_POST_METADATA_CONCURRENCY)),
                ),
            ),
        )
        video_payload = {
            "code": 0,
            "request_ids": video_request_ids,
            "batch_count": (len(ids) + TIKTOK_VIDEO_HASHTAG_BATCH_LIMIT - 1) // TIKTOK_VIDEO_HASHTAG_BATCH_LIMIT,
            "pagination_available": False,
            "candidate_limit_per_hashtag": TIKTOK_RANKED_VIDEO_LIMIT,
            "data": {"list": video_groups},
        }
        result = self.repository.save_snapshot(
            country_code=country, date_range=window, category_name=category,
            hashtag_payload=filtered_hashtag_payload, video_payload=video_payload,
            hashtag_diagnostics=hashtag_diagnostics,
        )
        # Retain legacy names for existing operation-result consumers while the
        # persisted/API diagnostics expose the more explicit normalized fields.
        hashtag_diagnostics["total_retrieved"] = filter_result.total_count
        hashtag_diagnostics["returned"] = len(filter_result.selected)
        result["hashtag_filter"] = hashtag_diagnostics
        return result

    def page(
        self,
        country_code: str = "ID",
        date_range: str = "1DAY",
        category_name: str = DEFAULT_TREND_CATEGORY,
    ) -> dict[str, Any]:
        country, window, category = _country_code(country_code), str(date_range).upper(), str(category_name).upper()
        snapshot = self.repository.latest_snapshot(country, window, category)
        warnings: list[str] = []
        config = self.configuration()
        if not config["access_configured"]:
            oauth = config.get("oauth") or {}
            warnings.append(
                str(oauth.get("configuration_error") or oauth.get("storage_error") or oauth.get("invalid_reason") or
                    "TikTok authorization or advertiser selection is required.")
            )
        if not config["media_dir_exists"]:
            warnings.append("Trend media directory does not exist.")
        if not config["ytdlp_available"]:
            warnings.append("yt-dlp is unavailable in the backend Python environment.")
        if not config["disk_reserve_satisfied"]:
            warnings.append("Trend media storage has less than the configured free-disk reserve.")
        if snapshot is None:
            return {
                "configuration": config,
                "snapshot": None,
                "hashtags": [],
                "hashtag_diagnostics": {
                    "source": TIKTOK_TREND_SOURCE,
                    "source_category": category,
                    "total_candidates_returned": 0,
                    "accepted_topical": 0,
                    "accepted_brands": 0,
                    "excluded": 0,
                    "deduplicated": 0,
                    "stored": 0,
                    "backend_returned": 0,
                    "selection_limit": MAX_RELEVANT_HASHTAGS,
                    "classifications": [],
                    "exclusions": [],
                },
                "videos": [],
                "video_diagnostics": [],
                "download_summary": None,
                "latest_pattern": None,
                "warnings": warnings,
            }
        snapshot_id = str(snapshot["snapshot_id"])
        database_hashtags = self.repository.hashtags(snapshot_id)
        page_filter = filter_relevant_hashtags(
            database_hashtags,
            classifier=self.hashtag_classifier,
            emit_diagnostics=False,
            default_source=TIKTOK_TREND_SOURCE,
            default_source_category=category,
        )
        hashtags = page_filter.selected
        snapshot_payload = _json_object(snapshot.get("payload_json"))
        hashtag_diagnostics = dict(snapshot_payload.get("hashtag_filter") or {})
        if not hashtag_diagnostics:
            hashtag_diagnostics = {
                "source": TIKTOK_TREND_SOURCE,
                "source_category": category,
                "total_candidates_returned": len(database_hashtags),
                "accepted_topical": page_filter.topical_count,
                "accepted_brands": page_filter.brand_count,
                "excluded": page_filter.excluded_count,
                "deduplicated": page_filter.deduplicated_count,
                "stored": len(database_hashtags),
                "selection_limit": MAX_RELEVANT_HASHTAGS,
                "classifications": [],
                "exclusions": [],
            }
        hashtag_diagnostics["backend_returned"] = len(hashtags)
        hashtag_diagnostics["selection_limit"] = MAX_RELEVANT_HASHTAGS
        if len(hashtags) < MAX_RELEVANT_HASHTAGS:
            warnings.append(
                f"TikTok returned {int(hashtag_diagnostics.get('total_candidates_returned') or len(database_hashtags))} "
                f"hashtag candidates, but only {len(hashtags)} unique relevant trends qualified; "
                "unrelated hashtags were not used to fill the list."
            )
        hashtag_ids = {str(item["hashtag_id"]) for item in hashtags}
        candidate_video_ids = {
            str(item["video_id"])
            for item in self.repository.candidates(snapshot_id)
            if str(item["hashtag_id"]) in hashtag_ids
        }
        for video_id in candidate_video_ids:
            self._reusable_canonical_media(video_id, actor="system:trend-page")
        valid_rows = [
            item for item in self.repository.videos(snapshot_id)
            if str(item["hashtag_id"]) in hashtag_ids
        ]
        videos = _first_ranked_videos_by_hashtag(valid_rows)
        for video in videos:
            video["original_provider_rank"] = int(video.get("provider_ordinal") or 0)
        candidates = [
            item for item in self.repository.candidates(snapshot_id)
            if str(item["hashtag_id"]) in hashtag_ids
        ]
        if candidates and all(
            str(item.get("classification_evidence") or "").startswith("legacy record")
            for item in candidates
        ):
            warnings.append(
                "This trend snapshot predates TikTok media classification. Refresh Discovery to classify its ranked posts."
            )
        diagnostics = _video_diagnostics_for_frontend(
            hashtags,
            candidates,
            videos,
            pagination_available=False,
        )
        logger.info(
            "TikTok hashtag frontend payload: snapshot_id=%s database_count=%d sent_to_frontend=%d "
            "frontend_display_limit=%d valid_video_count=%d diagnostics=%s",
            snapshot_id,
            len(database_hashtags),
            len(hashtags),
            MAX_RELEVANT_HASHTAGS,
            len(videos),
            [
                {
                    "hashtag": item["hashtag_name"],
                    "candidates": item["total_candidates_returned"],
                    "valid": item["valid_videos_stored"],
                    "sent": item["sent_to_frontend"],
                }
                for item in diagnostics
            ],
        )
        return {
            "configuration": config,
            "snapshot": {key: value for key, value in snapshot.items() if key != "payload_json"},
            "hashtags": hashtags,
            "hashtag_diagnostics": hashtag_diagnostics,
            "videos": videos,
            "video_diagnostics": diagnostics,
            "download_summary": self.repository.download_summary(snapshot_id, video_rows=videos),
            "latest_pattern": self.repository.latest_pattern(snapshot_id),
            "warnings": warnings,
        }

    def media_files(self) -> dict[str, Any]:
        root = self.media_root
        if not root.is_dir():
            return {"root": str(root), "exists": False, "files": []}
        files = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in SUPPORTED_TREND_MEDIA_EXTENSIONS:
                continue
            stat = path.stat()
            files.append({"relative_path": path.relative_to(root).as_posix(), "name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
            if len(files) >= 2000:
                break
        return {"root": str(root), "exists": True, "files": files}

    def link_media(self, video_id: str, relative_path: str, actor: str) -> dict[str, Any]:
        root = self.media_root
        try:
            path = resolve_within_root(root, relative_path, must_exist=True, kind="file")
        except UnsafePathError as exc:
            raise TrendServiceError(str(exc)) from exc
        if path.suffix.casefold() not in SUPPORTED_TREND_MEDIA_EXTENSIONS:
            raise TrendServiceError("Unsupported trend media extension.")
        if not self.database.scalar(
            "SELECT 1 FROM trend_videos WHERE video_id=? AND media_type='video' AND is_available=1 LIMIT 1",
            (video_id,),
            None,
        ):
            raise TrendServiceError("Unknown TikTok video reference.")
        _validate_video(path)
        stat = path.stat()
        digest = _file_sha256(path)
        return self.repository.link_media(video_id, path.relative_to(root).as_posix(), digest, stat, actor)

    def download_all(
        self,
        request: Any,
        *,
        run_id: str | None = None,
        actor: str = "system:trend-download",
    ) -> dict[str, Any]:
        if request.rights_confirmed is not True:
            raise TrendServiceError("Permission to download and store these videos must be confirmed.")
        snapshot_id = str(request.snapshot_id or "").strip()
        if self.repository.snapshot(snapshot_id) is None:
            raise TrendServiceError("Trend snapshot was not found.")
        return self._download_all_canonical(
            request, snapshot_id=snapshot_id, run_id=str(run_id or uuid4().hex), actor=actor
        )

    def _download_all_canonical(
        self,
        request: Any,
        *,
        snapshot_id: str,
        run_id: str,
        actor: str,
    ) -> dict[str, Any]:
        relevant_hashtag_ids = {
            str(item["hashtag_id"])
            for item in filter_relevant_hashtags(
                self.repository.hashtags(snapshot_id),
                classifier=self.hashtag_classifier,
                emit_diagnostics=False,
            ).selected
        }
        rows = [
            item
            for item in _first_ranked_videos_by_hashtag(self.repository.videos(snapshot_id))
            if str(item["hashtag_id"]) in relevant_hashtag_ids
        ]
        if not rows:
            raise TrendServiceError("The selected trend snapshot has no video references to download.")

        references: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            references.setdefault(str(row["video_id"]), []).append(row)

        self.media_root.mkdir(parents=True, exist_ok=True)
        library = resolve_within_root(self.media_root, "library", must_exist=False)
        library.mkdir(parents=True, exist_ok=True)
        media_by_id: dict[str, dict[str, Any]] = {}
        integrity_conflicts: dict[str, str] = {}
        for video_id in references:
            media, conflict = self._reusable_canonical_media(video_id, actor=actor)
            if media is not None:
                media_by_id[video_id] = media
            elif conflict:
                integrity_conflicts[video_id] = conflict

        retry_failed = bool(getattr(request, "retry_failed", False))
        skipped_errors: dict[str, str] = {}
        for video_id, video_rows in references.items():
            previous = self.repository.download(
                snapshot_id, str(video_rows[0]["hashtag_id"]), video_id
            )
            if (
                video_id not in media_by_id
                and video_id not in integrity_conflicts
                and previous is not None
                and str(previous.get("status")) == "failed"
                and not retry_failed
            ):
                skipped_errors[video_id] = str(
                    previous.get("error") or "Previous download failed; retry was not requested."
                )
        new_ids = [
            video_id for video_id in references
            if video_id not in media_by_id
            and video_id not in integrity_conflicts
            and video_id not in skipped_errors
        ]
        extractor_version = ""
        reserve = max(0, int(getattr(self.cfg, "TREND_YTDLP_MIN_FREE_BYTES", 5 * 1024**3)))
        timeout = max(30, int(getattr(self.cfg, "TREND_YTDLP_TIMEOUT_SECONDS", 600)))
        concurrency = max(1, min(4, int(getattr(self.cfg, "TREND_YTDLP_CONCURRENCY", 2))))
        circuit = DownloadCircuitBreaker(int(getattr(
            self.cfg, "TREND_DOWNLOAD_CIRCUIT_BREAKER_THRESHOLD",
            TIKTOK_DOWNLOAD_CIRCUIT_BREAKER_THRESHOLD,
        )))
        downloaded_ids: set[str] = set()
        failures: list[dict[str, str]] = []
        network_errors: dict[str, str] = {}

        if new_ids:
            available, extractor_version = self._ytdlp_configuration()
            if not available:
                raise TrendServiceError("yt-dlp is unavailable in the backend Python environment.")
            if _free_disk_bytes(self.media_root) < reserve:
                raise TrendServiceError("Trend media storage does not meet the configured free-disk reserve.")

            first_id = new_ids[0]
            first_row = references[first_id][0]
            try:
                source_url = _validated_tiktok_video_url(str(first_row.get("share_url") or ""), first_id)
                media_by_id[first_id] = self._acquire_canonical_media(
                    first_row, source_url=source_url, timeout=timeout, reserve=reserve,
                    actor=actor,
                )
                downloaded_ids.add(first_id)
                circuit.success()
            except Exception as exc:
                error = _sanitize_download_error(f"{type(exc).__name__}: {exc}")
                network_errors[first_id] = error
                failure = circuit.failed(error)
                if failure.systemic:
                    self._record_canonical_failure(references[first_id], run_id, error)
                    interruption = (
                        "Bulk download stopped because the preflight detected a shared downloader failure. "
                        f"{failure.message}"
                    )
                    self._interrupt_canonical_references(
                        (row for video_id in new_ids[1:] for row in references[video_id]),
                        run_id=run_id, error=interruption,
                    )
                    raise TrendServiceError(
                        "TikTok downloads are currently unavailable: the configured downloader "
                        f"failed during preflight. Details: {failure.message}"
                    ) from exc

            remaining = [video_id for video_id in new_ids[1:] if video_id not in media_by_id]
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="trend-download") as executor:
                offset = 0
                while offset < len(remaining) and not circuit.tripped:
                    wave = remaining[offset:offset + concurrency]
                    pending = {}
                    for video_id in wave:
                        row = references[video_id][0]
                        try:
                            source_url = _validated_tiktok_video_url(
                                str(row.get("share_url") or ""), video_id
                            )
                        except TrendServiceError as exc:
                            network_errors[video_id] = _sanitize_download_error(str(exc))
                            continue
                        pending[executor.submit(
                            self._acquire_canonical_media, row, source_url=source_url,
                            timeout=timeout, reserve=reserve, actor=actor,
                        )] = video_id
                    for future in as_completed(pending):
                        video_id = pending[future]
                        try:
                            media_by_id[video_id] = future.result()
                            downloaded_ids.add(video_id)
                            circuit.success()
                        except Exception as exc:
                            error = _sanitize_download_error(f"{type(exc).__name__}: {exc}")
                            network_errors[video_id] = error
                            circuit.failed(error)
                    offset += len(wave)

                if circuit.tripped and offset < len(remaining):
                    untouched = remaining[offset:]
                    interruption = (
                        "Bulk download stopped because a shared downloader failure was detected. "
                        f"{circuit.failure.message if circuit.failure else 'TikTok downloader failed repeatedly.'}"
                    )
                    self._interrupt_canonical_references(
                        (row for video_id in untouched for row in references[video_id]),
                        run_id=run_id, error=interruption,
                    )
                    for video_id in remaining[:offset]:
                        if video_id in network_errors:
                            self._record_canonical_failure(
                                references[video_id], run_id, network_errors[video_id]
                            )
                    for video_id, media in media_by_id.items():
                        for index, row in enumerate(references[video_id]):
                            was_network_download = video_id in downloaded_ids and index == 0
                            self.repository.record_reference(
                                row=row, run_id=run_id,
                                source_url=str(row.get("share_url") or ""),
                                status="downloaded" if was_network_download else "reused",
                                media=media, extractor_version=extractor_version,
                                attempted=was_network_download,
                            )
                    self._write_canonical_hashtag_metadata(rows, media_by_id)
                    raise TrendServiceError(
                        "TikTok downloader failed repeatedly. "
                        f"{len(untouched)} videos were not attempted. "
                        f"Details: {circuit.failure.message if circuit.failure else interruption}"
                    )

        for video_id, error in integrity_conflicts.items():
            self._record_canonical_failure(references[video_id], run_id, error, attempted=False)
            failures.append({"video_id": video_id, "error": error})
        for video_id, error in skipped_errors.items():
            self._record_canonical_failure(
                references[video_id], run_id, error, attempted=False
            )
            failures.append({"video_id": video_id, "error": error})
        for video_id, error in network_errors.items():
            if video_id not in downloaded_ids:
                self._record_canonical_failure(references[video_id], run_id, error)
                failures.append({"video_id": video_id, "error": error})

        reused_ids = set(media_by_id) - downloaded_ids
        for video_id, media in media_by_id.items():
            for index, row in enumerate(references[video_id]):
                was_network_download = video_id in downloaded_ids and index == 0
                self.repository.record_reference(
                    row=row, run_id=run_id,
                    source_url=str(row.get("share_url") or ""),
                    status="downloaded" if was_network_download else "reused",
                    media=media, extractor_version=extractor_version,
                    attempted=was_network_download,
                )

        self._write_canonical_hashtag_metadata(rows, media_by_id)
        result = {
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "target_reference_count": len(rows),
            "unique_video_count": len(references),
            "saved_count": len(media_by_id),
            "approved_count": len(media_by_id),
            "downloaded_count": len(downloaded_ids),
            "reused_count": len(reused_ids),
            "new_count": len(references) - len(media_by_id),
            "failed_count": len(failures),
            "failures": failures,
            "bytes_written": sum(
                int(media_by_id[video_id]["file_size"]) for video_id in downloaded_ids
            ),
            "library": str(library),
        }
        if not media_by_id and failures:
            raise TrendServiceError(
                "No TikTok videos could be downloaded or reused. See the per-video download errors."
            )
        return result

    def _canonical_relative_path(self, video_id: str) -> str:
        trend_download_filename(1, video_id)
        return (Path("library") / f"{video_id}.mp4").as_posix()

    def _reusable_canonical_media(
        self, video_id: str, *, actor: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        relative_path = self._canonical_relative_path(video_id)
        path = resolve_within_root(self.media_root, relative_path, must_exist=False)
        link = self.repository.media_link(video_id)
        if not path.is_file():
            if link is not None:
                self.repository.set_media_status(
                    video_id, "missing", "Canonical media file is missing."
                )
            return None, None
        try:
            stat = path.stat()
            if (
                link is not None
                and str(link.get("relative_path")) == relative_path
                and int(link.get("file_size") or -1) == int(stat.st_size)
                and int(link.get("file_mtime_ns") or -1) == int(stat.st_mtime_ns)
                and str(link.get("status") or "") in {"media_ready", "analyzing", "analyzed"}
            ):
                return {**link, "duration_seconds": 0.0}, None
            probe = _probe_downloaded_video(path)
            digest = _file_sha256(path)
            if link is not None and link.get("file_sha256") and digest != link["file_sha256"]:
                error = "Canonical media changed unexpectedly; local integrity conflict requires review."
                self.repository.set_media_status(video_id, "integrity_conflict", error)
                return None, error
            linked = self.repository.link_media(video_id, relative_path, digest, stat, actor)
            return {**linked, "file_size": stat.st_size, "file_mtime_ns": stat.st_mtime_ns,
                    "duration_seconds": float(probe["duration_seconds"])}, None
        except (OSError, TrendServiceError) as exc:
            return None, _sanitize_download_error(str(exc))

    def _acquire_canonical_media(
        self,
        row: dict[str, Any],
        *,
        source_url: str,
        timeout: int,
        reserve: int,
        actor: str,
    ) -> dict[str, Any]:
        video_id = str(row["video_id"])
        lock_key = (os.path.normcase(str(self.media_root)), video_id)
        with _VIDEO_ACQUISITION_LOCKS_GUARD:
            lock = _VIDEO_ACQUISITION_LOCKS.setdefault(lock_key, threading.Lock())
        with lock:
            cached, conflict = self._reusable_canonical_media(video_id, actor=actor)
            if cached is not None:
                return cached
            if conflict:
                raise TrendServiceError(conflict)
            if _free_disk_bytes(self.media_root) < reserve:
                raise TrendServiceError("Free-disk reserve reached before download started.")
            library = resolve_within_root(self.media_root, "library", must_exist=False)
            library.mkdir(parents=True, exist_ok=True)
            work_dir = resolve_within_root(
                self.media_root, f"library/.{video_id}.{uuid4().hex}.download", must_exist=False
            )
            work_dir.mkdir(parents=False, exist_ok=False)
            try:
                produced = Path(self.download_runner(video_id, source_url, work_dir, timeout)).resolve()
                try:
                    produced.relative_to(work_dir)
                except ValueError as exc:
                    raise TrendServiceError(
                        "yt-dlp returned a file outside its temporary download directory."
                    ) from exc
                if not produced.is_file() or produced.suffix.casefold() != ".mp4":
                    raise TrendServiceError("yt-dlp did not produce a completed MP4 video file.")
                probe = _probe_downloaded_video(produced)
                digest = _file_sha256(produced)
                target = resolve_within_root(
                    self.media_root, self._canonical_relative_path(video_id), must_exist=False
                )
                os.replace(produced, target)
                stat = target.stat()
                linked = self.repository.link_media(
                    video_id, self._canonical_relative_path(video_id), digest, stat, actor
                )
                return {
                    **linked, "file_size": int(stat.st_size),
                    "file_mtime_ns": int(stat.st_mtime_ns),
                    "duration_seconds": float(probe["duration_seconds"]),
                }
            finally:
                if work_dir.exists():
                    shutil.rmtree(work_dir)

    def _record_canonical_failure(
        self,
        rows: list[dict[str, Any]],
        run_id: str,
        error: str,
        attempted: bool = True,
    ) -> None:
        for index, row in enumerate(rows):
            self.repository.record_failed_reference(
                row=row, run_id=run_id, source_url=str(row.get("share_url") or ""),
                error=error, attempted=attempted and index == 0,
            )

    def _interrupt_canonical_references(
        self, rows: Iterable[dict[str, Any]], *, run_id: str, error: str
    ) -> int:
        count = 0
        for row in rows:
            self.repository.interrupt_download(
                row=row, run_id=run_id, source_url=str(row.get("share_url") or ""), error=error
            )
            count += 1
        return count

    def _write_canonical_hashtag_metadata(
        self, rows: list[dict[str, Any]], media_by_id: dict[str, dict[str, Any]]
    ) -> None:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(str(row["hashtag_id"]), []).append(row)
        for hashtag_rows in groups.values():
            normalized = normalize_hashtag_folder_name(hashtag_rows[0].get("hashtag_name"))
            folder = resolve_within_root(
                self.media_root, f"hashtags/{normalized}", must_exist=False
            )
            folder.mkdir(parents=True, exist_ok=True)
            videos = [
                {
                    "rank": int(row["final_rank"]),
                    "video_id": str(row["video_id"]),
                    "media": f"../../library/{row['video_id']}.mp4",
                    "share_url": str(row.get("share_url") or ""),
                }
                for row in hashtag_rows if str(row["video_id"]) in media_by_id
            ]
            target = folder / "metadata.json"
            temporary = folder / f".metadata.{uuid4().hex}.tmp"
            temporary.write_text(json.dumps({
                "hashtag": normalized,
                "source_hashtag": str(hashtag_rows[0].get("hashtag_name") or ""),
                "snapshot_id": str(hashtag_rows[0]["snapshot_id"]),
                "updated_at": utc_now(),
                "videos": videos,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, target)

    def audit_legacy_media(self, legacy_root: Path | None = None) -> dict[str, Any]:
        root = (legacy_root or self.legacy_media_root).resolve()
        files = sorted(root.rglob("*.mp4")) if root.is_dir() else []
        path_ids: dict[str, str] = {}
        for table in ("trend_media_downloads", "trend_media_links"):
            for row in self.database.query(
                f"SELECT video_id,relative_path FROM {table} WHERE relative_path IS NOT NULL"
            ):
                try:
                    path = resolve_within_root(root, str(row["relative_path"]), must_exist=False)
                except UnsafePathError:
                    continue
                path_ids[os.path.normcase(str(path))] = str(row["video_id"])

        groups: dict[str, list[dict[str, Any]]] = {}
        invalid: list[dict[str, str]] = []
        unknown: list[str] = []
        for path in files:
            video_id = path_ids.get(os.path.normcase(str(path.resolve())))
            if video_id is None:
                match = re.fullmatch(r"(?:\d{3}_)?([A-Za-z0-9_-]{1,128})\.mp4", path.name)
                if match:
                    video_id = match.group(1)
            if video_id is None:
                unknown.append(str(path))
                continue
            try:
                probe = _probe_downloaded_video(path)
                digest = _file_sha256(path)
                stat = path.stat()
                groups.setdefault(video_id, []).append({
                    "path": path, "sha256": digest, "size": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                    "duration_seconds": float(probe["duration_seconds"]),
                })
            except (OSError, TrendServiceError) as exc:
                invalid.append({"path": str(path), "error": _sanitize_download_error(str(exc))})

        conflicts = []
        duplicate_file_count = 0
        duplicate_bytes = 0
        for video_id, items in groups.items():
            hashes: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                hashes.setdefault(str(item["sha256"]), []).append(item)
            if len(hashes) > 1:
                conflicts.append({
                    "video_id": video_id,
                    "hashes": {digest: [str(item["path"]) for item in matches]
                               for digest, matches in hashes.items()},
                })
            for matches in hashes.values():
                duplicate_file_count += max(0, len(matches) - 1)
                duplicate_bytes += sum(int(item["size"]) for item in matches[1:])
        missing_links = 0
        for link in self.database.query("SELECT relative_path FROM trend_media_links"):
            try:
                present = resolve_within_root(
                    root, str(link["relative_path"]), must_exist=False
                ).is_file()
            except UnsafePathError:
                present = False
            if not present:
                missing_links += 1
        return {
            "root": str(root), "physical_mp4_count": len(files),
            "physical_bytes": sum(path.stat().st_size for path in files),
            "unique_video_ids": len(groups), "duplicate_video_ids": sum(
                1 for items in groups.values() if len(items) > 1
            ),
            "duplicate_file_count": duplicate_file_count,
            "duplicate_bytes": duplicate_bytes,
            "missing_database_links": missing_links,
            "invalid_files": invalid, "unknown_files": unknown,
            "hash_conflicts": conflicts, "groups": groups,
        }

    def migrate_legacy_media(
        self,
        legacy_root: Path | None = None,
        *,
        cleanup: bool = False,
        actor: str = "system:trend-media-migration",
    ) -> dict[str, Any]:
        audit = self.audit_legacy_media(legacy_root)
        root = Path(audit["root"])
        library = resolve_within_root(self.media_root, "library", must_exist=False)
        library.mkdir(parents=True, exist_ok=True)
        migrated = 0
        reused = 0
        removed = 0
        reclaimed = 0
        conflicts: list[dict[str, Any]] = []
        left_behind: list[str] = list(audit["unknown_files"])
        conflict_ids = {item["video_id"] for item in audit["hash_conflicts"]}

        for video_id, items in audit["groups"].items():
            target = resolve_within_root(
                self.media_root, self._canonical_relative_path(video_id), must_exist=False
            )
            link = self.repository.media_link(video_id)
            chosen: dict[str, Any] | None = None
            if video_id in conflict_ids:
                expected_hash = str(link.get("file_sha256") or "") if link else ""
                authoritative = [item for item in items if item["sha256"] == expected_hash]
                if len({item["sha256"] for item in authoritative}) == 1 and authoritative:
                    chosen = authoritative[0]
                else:
                    conflicts.append(next(
                        item for item in audit["hash_conflicts"] if item["video_id"] == video_id
                    ))
                    left_behind.extend(str(item["path"]) for item in items)
                    continue
            else:
                chosen = items[0]

            if target.is_file():
                try:
                    target_hash = _file_sha256(target)
                    _probe_downloaded_video(target)
                except (OSError, TrendServiceError):
                    target_hash = ""
                if target_hash != chosen["sha256"]:
                    conflicts.append({
                        "video_id": video_id,
                        "reason": "canonical target differs from validated legacy source",
                        "paths": [str(target), str(chosen["path"])],
                    })
                    left_behind.extend(str(item["path"]) for item in items)
                    continue
                reused += 1
            else:
                temporary = library / f".{video_id}.{uuid4().hex}.migration"
                try:
                    shutil.copy2(chosen["path"], temporary)
                    with temporary.open("r+b") as handle:
                        os.fsync(handle.fileno())
                    if _file_sha256(temporary) != chosen["sha256"]:
                        raise TrendServiceError("Migrated media checksum did not match its source.")
                    _probe_downloaded_video(temporary)
                    os.replace(temporary, target)
                    migrated += 1
                finally:
                    if temporary.exists():
                        temporary.unlink()

            stat = target.stat()
            self.repository.link_media(
                video_id, self._canonical_relative_path(video_id), chosen["sha256"], stat, actor
            )
            canonical_hash = _file_sha256(target)
            _probe_downloaded_video(target)
            if cleanup:
                for item in items:
                    source = Path(item["path"])
                    if (
                        source.is_file()
                        and item["sha256"] == canonical_hash
                        and _is_relative_to(source, root)
                    ):
                        size = source.stat().st_size
                        source.unlink()
                        removed += 1
                        reclaimed += size
                    else:
                        left_behind.append(str(source))

        canonical_ids = {
            path.stem for path in library.glob("*.mp4") if path.is_file()
        }
        for link in self.database.query("SELECT video_id,status FROM trend_media_links"):
            video_id = str(link["video_id"])
            if video_id not in canonical_ids and str(link["status"]) != "missing":
                self.repository.set_media_status(
                    video_id, "missing", "No validated canonical media file is present."
                )

        return {
            **{key: value for key, value in audit.items() if key != "groups"},
            "new_root": str(self.media_root), "library": str(library),
            "files_migrated": migrated, "canonical_files_reused": reused,
            "source_files_removed": removed, "source_bytes_removed": reclaimed,
            "bytes_reclaimed": int(audit["duplicate_bytes"]) if cleanup else 0,
            "conflicts": conflicts, "files_left_behind": sorted(set(left_behind)),
        }

    def _existing_download_source(self, row: dict[str, Any]) -> Path | None:
        downloads_root = resolve_within_root(self.media_root, "downloads", must_exist=False)
        normalized_hashtag = normalize_hashtag_folder_name(row.get("hashtag_name"))
        final_dir = resolve_within_root(
            self.media_root, f"downloads/{normalized_hashtag}", must_exist=False
        )
        video_id = str(row["video_id"])
        expected_name = trend_download_filename(int(row["final_rank"]), video_id)
        return (
            _find_existing_ranked_video(final_dir, expected_name, video_id)
            or _find_legacy_video(downloads_root, video_id)
        )

    def _interrupt_rows(self, rows: Iterable[dict[str, Any]], *, run_id: str, error: str) -> int:
        interrupted = 0
        for row in rows:
            if self._existing_download_source(row) is not None:
                continue
            video_id = str(row["video_id"])
            try:
                source_url = _validated_tiktok_video_url(str(row.get("share_url") or ""), video_id)
            except TrendServiceError:
                source_url = ""
            self.repository.interrupt_download(
                row=row, run_id=run_id, source_url=source_url, error=error
            )
            interrupted += 1
        return interrupted

    def _preflight_download(
        self,
        *,
        rows: list[dict[str, Any]],
        run_id: str,
        retry_failed: bool,
        reserve: int,
        timeout: int,
    ) -> tuple[Path | None, dict[tuple[str, str], Path], dict[tuple[str, str], dict[str, str]]]:
        candidates: list[tuple[dict[str, Any], str]] = []
        for row in rows:
            if self._existing_download_source(row) is not None:
                continue
            existing = self.repository.download(
                str(row["snapshot_id"]), str(row["hashtag_id"]), str(row["video_id"])
            )
            if existing and existing.get("status") == "failed" and not retry_failed:
                continue
            video_id = str(row["video_id"])
            try:
                source_url = _validated_tiktok_video_url(str(row.get("share_url") or ""), video_id)
            except TrendServiceError:
                continue
            candidates.append((row, source_url))
        if not candidates:
            return None, {}, {}

        downloads_root = resolve_within_root(self.media_root, "downloads", must_exist=False)
        downloads_root.mkdir(parents=True, exist_ok=True)
        preflight_dir = resolve_within_root(
            self.media_root, f"downloads/.preflight.{uuid4().hex}.tmp", must_exist=False
        )
        preflight_dir.mkdir(parents=False, exist_ok=False)
        failures: dict[tuple[str, str], dict[str, str]] = {}
        try:
            for row, source_url in candidates[:3]:
                video_id = str(row["video_id"])
                hashtag_id = str(row["hashtag_id"])
                self.repository.queue_download(
                    video_id=video_id,
                    snapshot_id=str(row["snapshot_id"]),
                    hashtag_id=hashtag_id,
                    hashtag_name=str(row.get("hashtag_name") or ""),
                    normalized_hashtag=normalize_hashtag_folder_name(row.get("hashtag_name")),
                    final_rank=int(row["final_rank"]),
                    run_id=run_id,
                    source_url=source_url,
                )
                try:
                    record = self._download_one_to_stage(
                        str(row["snapshot_id"]), hashtag_id, row, source_url,
                        preflight_dir, trend_download_filename(int(row["final_rank"]), video_id),
                        reserve, timeout, normalize_hashtag_folder_name(row.get("hashtag_name")),
                    )
                    path = preflight_dir / Path(record["relative_path"]).name
                    return preflight_dir, {(hashtag_id, video_id): path}, failures
                except Exception as exc:
                    error = _sanitize_download_error(f"{type(exc).__name__}: {exc}")
                    self.repository.fail_download(str(row["snapshot_id"]), hashtag_id, video_id, error)
                    failure = _classify_download_error(error)
                    failures[(hashtag_id, video_id)] = {"video_id": video_id, "error": error}
                    if failure.systemic:
                        interruption = (
                            "Bulk download stopped because the preflight detected a shared downloader failure. "
                            f"{failure.message}"
                        )
                        self._interrupt_rows(
                            (
                                item for item in rows
                                if (str(item["hashtag_id"]), str(item["video_id"]))
                                != (hashtag_id, video_id)
                            ),
                            run_id=run_id,
                            error=interruption,
                        )
                        raise TrendServiceError(
                            "TikTok downloads are currently unavailable: the configured downloader "
                            f"failed during preflight. Details: {failure.message}"
                        ) from exc
            failed_keys = set(failures)
            self._interrupt_rows(
                (
                    item for item in rows
                    if (str(item["hashtag_id"]), str(item["video_id"])) not in failed_keys
                ),
                run_id=run_id,
                error="Bulk download stopped because no available video passed preflight.",
            )
            raise TrendServiceError(
                "TikTok downloads could not start: no available video passed the three-item preflight."
            )
        except BaseException:
            if preflight_dir.exists():
                shutil.rmtree(preflight_dir)
            raise

    def _refresh_hashtag_folder(
        self,
        *,
        snapshot_id: str,
        rows: list[dict[str, Any]],
        run_id: str,
        retry_failed: bool,
        extractor_version: str,
        reserve: int,
        timeout: int,
        concurrency: int,
        actor: str,
        circuit: DownloadCircuitBreaker,
        preflight_files: dict[tuple[str, str], Path],
        preflight_failures: dict[tuple[str, str], dict[str, str]],
    ) -> dict[str, Any]:
        hashtag_id = str(rows[0]["hashtag_id"])
        hashtag_name = str(rows[0].get("hashtag_name") or "")
        normalized_hashtag = normalize_hashtag_folder_name(hashtag_name)
        downloads_root = resolve_within_root(self.media_root, "downloads", must_exist=False)
        downloads_root.mkdir(parents=True, exist_ok=True)
        final_dir = resolve_within_root(
            self.media_root, f"downloads/{normalized_hashtag}", must_exist=False
        )
        legacy_video_file = final_dir / f"{final_dir.name}.mp4"
        if (
            final_dir.is_dir()
            and legacy_video_file.is_file()
            and not (final_dir / "metadata.json").exists()
        ):
            raise TrendServiceError(
                f"The hashtag folder '{normalized_hashtag}' collides with a legacy video-ID folder; "
                "the legacy folder was preserved and no refresh was attempted."
            )
        stage_dir = resolve_within_root(
            self.media_root,
            f"downloads/.{normalized_hashtag}.{uuid4().hex}.tmp",
            must_exist=False,
        )
        stage_dir.mkdir(parents=False, exist_ok=False)
        prepared: dict[str, dict[str, Any]] = {}
        downloads: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        reused = 0
        downloaded = 0
        bytes_written = 0
        moved = False
        backup_dir: Path | None = None
        try:
            for row in rows:
                video_id = str(row["video_id"])
                download_key = (hashtag_id, video_id)
                final_rank = int(row["final_rank"])
                expected_name = trend_download_filename(final_rank, video_id)
                existing_record = self.repository.download(snapshot_id, hashtag_id, video_id)
                preflight_source = preflight_files.get(download_key)
                source = preflight_source
                if source is None:
                    source = _find_existing_ranked_video(final_dir, expected_name, video_id)
                if source is None:
                    source = _find_legacy_video(downloads_root, video_id)
                source_url = ""
                if source is None:
                    if download_key in preflight_failures:
                        failures.append(preflight_failures[download_key])
                    elif existing_record and existing_record.get("status") == "failed" and not retry_failed:
                        failures.append({
                            "video_id": video_id,
                            "error": _sanitize_download_error(
                                str(existing_record.get("error") or "Previous download failed.")
                            ),
                        })
                    else:
                        try:
                            source_url = _validated_tiktok_video_url(
                                str(row.get("share_url") or ""), video_id
                            )
                        except TrendServiceError as exc:
                            failures.append({
                                "video_id": video_id,
                                "error": _sanitize_download_error(str(exc)),
                            })
                if preflight_source is None and download_key not in preflight_failures:
                    self.repository.queue_download(
                        video_id=video_id,
                        snapshot_id=snapshot_id,
                        hashtag_id=hashtag_id,
                        hashtag_name=hashtag_name,
                        normalized_hashtag=normalized_hashtag,
                        final_rank=final_rank,
                        run_id=run_id,
                        source_url=source_url,
                    )
                if source is not None:
                    target = stage_dir / expected_name
                    shutil.copy2(source, target)
                    prepared[video_id] = self._prepared_download_record(
                        snapshot_id, hashtag_id, row, target, normalized_hashtag
                    )
                    if preflight_source is not None:
                        downloaded += 1
                        bytes_written += int(target.stat().st_size)
                    else:
                        reused += 1
                elif source_url:
                    downloads.append({
                        "row": row,
                        "source_url": source_url,
                        "target_name": expected_name,
                    })

            unattempted: list[dict[str, Any]] = []
            with ThreadPoolExecutor(
                max_workers=concurrency, thread_name_prefix="trend-download"
            ) as executor:
                for offset in range(0, len(downloads), concurrency):
                    if circuit.tripped:
                        unattempted.extend(downloads[offset:])
                        break
                    wave = downloads[offset:offset + concurrency]
                    pending = {
                        executor.submit(
                            self._download_one_to_stage,
                            snapshot_id,
                            hashtag_id,
                            item["row"],
                            item["source_url"],
                            stage_dir,
                            item["target_name"],
                            reserve,
                            timeout,
                            normalized_hashtag,
                        ): item
                        for item in wave
                    }
                    for future in as_completed(pending):
                        item = pending[future]
                        video_id = str(item["row"]["video_id"])
                        try:
                            record = future.result()
                            prepared[video_id] = record
                            downloaded += 1
                            bytes_written += int(record["file_size"])
                            circuit.success()
                        except Exception as exc:
                            error = _sanitize_download_error(f"{type(exc).__name__}: {exc}")
                            failures.append({"video_id": video_id, "error": error})
                            circuit.failed(error)
                    if circuit.tripped:
                        unattempted.extend(downloads[offset + len(wave):])
                        break

            interruption_error = ""
            if circuit.tripped:
                interruption_error = (
                    "Bulk download stopped because a shared downloader failure was detected. "
                    f"{circuit.failure.message if circuit.failure else 'TikTok downloader failed repeatedly.'}"
                )
                self._interrupt_rows(
                    (item["row"] for item in unattempted),
                    run_id=run_id,
                    error=interruption_error,
                )

            interrupted_ids = {
                str(item["row"]["video_id"])
                for item in unattempted
            }
            failed_ids = {item["video_id"] for item in failures}
            for row in rows:
                video_id = str(row["video_id"])
                if (
                    video_id not in prepared
                    and video_id not in failed_ids
                    and video_id not in interrupted_ids
                ):
                    failures.append({
                        "video_id": video_id,
                        "error": "The video could not be prepared for this hashtag refresh.",
                    })
                    failed_ids.add(video_id)

            if not prepared:
                failed_ids = {item["video_id"] for item in failures}
                for failure in failures:
                    if failure["video_id"]:
                        self.repository.fail_download(
                            snapshot_id, hashtag_id, failure["video_id"], failure["error"]
                        )
                for row in rows:
                    video_id = str(row["video_id"])
                    if video_id not in failed_ids and video_id not in interrupted_ids:
                        error = "Hashtag refresh aborted; previous successful folder was preserved."
                        self.repository.fail_download(snapshot_id, hashtag_id, video_id, error)
                return {
                    "hashtag": normalized_hashtag,
                    "candidate_count": len(rows),
                    "saved_count": 0,
                    "downloaded_count": 0,
                    "reused_count": 0,
                    "bytes_written": 0,
                    "folder": str(final_dir),
                    "stale_files_remaining": None,
                    "failures": failures or [{
                        "video_id": "",
                        "error": "Hashtag refresh did not prepare every ranked video.",
                    }],
                    "circuit_break": ({
                        "error": circuit.failure.message if circuit.failure else interruption_error,
                        "unattempted_count": len(unattempted),
                    } if circuit.tripped else None),
                }

            successful_rows: list[dict[str, Any]] = []
            ordered_records: list[dict[str, Any]] = []
            for row in rows:
                video_id = str(row["video_id"])
                record = prepared.get(video_id)
                if record is None:
                    continue
                ranked_row = dict(row)
                successful_rows.append(ranked_row)
                record["final_rank"] = int(ranked_row["final_rank"])
                ordered_records.append(record)

            for child in stage_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
            failed_errors = {
                str(item["video_id"]): str(item["error"])
                for item in failures
                if item["video_id"]
            }
            _write_hashtag_metadata(
                stage_dir, hashtag_name, normalized_hashtag, successful_rows
            )
            for record in ordered_records:
                _probe_downloaded_video(stage_dir / Path(record["relative_path"]).name)
            backup_dir = _replace_hashtag_directory(final_dir, stage_dir)
            moved = True
            try:
                for record in ordered_records:
                    committed_path = final_dir / Path(record["relative_path"]).name
                    stat = committed_path.stat()
                    record["file_size"] = int(stat.st_size)
                    record["file_mtime_ns"] = int(stat.st_mtime_ns)
                self.repository.complete_hashtag_downloads(
                    ordered_records,
                    ranked_rows=successful_rows,
                    failed_errors=failed_errors,
                    extractor_version=extractor_version,
                    actor=actor,
                )
            except BaseException:
                _restore_hashtag_directory(final_dir, backup_dir)
                moved = False
                raise
            if backup_dir is not None and backup_dir.exists():
                shutil.rmtree(backup_dir)
                backup_dir = None
            return {
                "hashtag": normalized_hashtag,
                "candidate_count": len(rows),
                "saved_count": len(successful_rows),
                "downloaded_count": downloaded,
                "reused_count": reused,
                "bytes_written": bytes_written,
                "folder": str(final_dir),
                "example_filenames": [Path(record["relative_path"]).name for record in ordered_records[:3]],
                "stale_files_remaining": False,
                "failures": failures,
                "circuit_break": ({
                    "error": circuit.failure.message if circuit.failure else interruption_error,
                    "unattempted_count": len(unattempted),
                } if circuit.tripped else None),
            }
        finally:
            if stage_dir.exists() and not moved:
                shutil.rmtree(stage_dir)

    def _download_one_to_stage(
        self,
        snapshot_id: str,
        hashtag_id: str,
        row: dict[str, Any],
        source_url: str,
        stage_dir: Path,
        target_name: str,
        reserve: int,
        timeout: int,
        normalized_hashtag: str,
    ) -> dict[str, Any]:
        if _free_disk_bytes(self.media_root) < reserve:
            raise TrendServiceError("Free-disk reserve reached before download started.")
        video_id = str(row["video_id"])
        self.repository.start_download(snapshot_id, hashtag_id, video_id)
        work_dir = stage_dir / f".{video_id}.download"
        work_dir.mkdir(parents=False, exist_ok=False)
        path = Path(self.download_runner(video_id, source_url, work_dir, timeout)).resolve()
        try:
            path.relative_to(work_dir)
        except ValueError as exc:
            raise TrendServiceError("yt-dlp returned a file outside its temporary download directory.") from exc
        if not path.is_file() or path.suffix.casefold() != ".mp4":
            raise TrendServiceError("yt-dlp did not produce a completed MP4 video file.")
        _probe_downloaded_video(path)
        target = stage_dir / target_name
        os.replace(path, target)
        shutil.rmtree(work_dir)
        return self._prepared_download_record(
            snapshot_id, hashtag_id, row, target, normalized_hashtag
        )

    def _prepared_download_record(
        self,
        snapshot_id: str,
        hashtag_id: str,
        row: dict[str, Any],
        path: Path,
        normalized_hashtag: str,
    ) -> dict[str, Any]:
        probe = _probe_downloaded_video(path)
        stat = path.stat()
        return {
            "snapshot_id": snapshot_id,
            "hashtag_id": hashtag_id,
            "video_id": str(row["video_id"]),
            "final_rank": int(row["final_rank"]),
            "relative_path": (
                Path("downloads") / normalized_hashtag / path.name
            ).as_posix(),
            "file_sha256": _file_sha256(path),
            "file_size": int(stat.st_size),
            "file_mtime_ns": int(stat.st_mtime_ns),
            "duration_seconds": float(probe["duration_seconds"]),
        }

    def analyze(self, request: Any) -> dict[str, Any]:
        snapshot = self.repository.snapshot(str(request.snapshot_id))
        if snapshot is None:
            raise TrendServiceError("Trend snapshot was not found.")
        video_ids = list(dict.fromkeys(str(item).strip() for item in request.video_ids if str(item).strip()))
        rows = self._selected_video_rows(str(request.snapshot_id), video_ids)
        if len(rows) != len(video_ids):
            raise TrendServiceError("One or more selected videos are not part of this snapshot.")
        hashtag_counts: dict[str, int] = {}
        for row in rows:
            hashtag_counts[row["hashtag_id"]] = hashtag_counts.get(row["hashtag_id"], 0) + 1
        if len(rows) < 5:
            raise TrendServiceError("At least five linked videos are required.")
        if len(hashtag_counts) < 3:
            raise TrendServiceError("Selected videos must cover at least three hashtags.")
        if max(hashtag_counts.values(), default=0) > 4:
            raise TrendServiceError("No more than four selected videos may come from one hashtag.")

        fingerprints: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for row in rows:
            video_id = row["video_id"]
            link = self.repository.media_link(video_id)
            if link is None:
                failures.append({"video_id": video_id, "error": "No approved media is linked."})
                continue
            try:
                path = resolve_within_root(self.media_root, link["relative_path"], must_exist=True, kind="file")
                stat = path.stat()
                current_hash = _file_sha256(path)
                if current_hash != link["file_sha256"] or int(stat.st_mtime_ns) != int(link["file_mtime_ns"]):
                    link = self.repository.link_media(video_id, link["relative_path"], current_hash, stat, link["approved_by"])
                self.repository.set_media_status(video_id, "analyzing")
                fingerprint = None if request.force else self.repository.cached_fingerprint(str(request.snapshot_id), video_id, current_hash)
                if fingerprint is None:
                    fingerprint = analyze_trend_video(video_id, path, current_hash, self.cfg)
                    fingerprint = self.repository.save_fingerprint(str(request.snapshot_id), video_id, row["hashtag_id"], current_hash, "completed", fingerprint)
                fingerprints.append(fingerprint)
                self.repository.set_media_status(video_id, "analyzed")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failures.append({"video_id": video_id, "error": error[:1000]})
                self.repository.set_media_status(video_id, "failed", error)
        if len(fingerprints) < 5:
            raise TrendServiceError(f"Only {len(fingerprints)} videos analyzed successfully; five are required.")
        pattern = _aggregate_pattern(str(request.snapshot_id), fingerprints, self.cfg)
        pattern["failures"] = failures
        saved = self.repository.save_pattern(str(request.snapshot_id), pattern["base_profile_revision"], pattern)
        return {
            "snapshot_id": str(request.snapshot_id),
            "analyzed_count": len(fingerprints),
            "failed_count": len(failures),
            "pattern_id": saved["pattern_id"],
            "warnings": [warning for item in fingerprints for warning in item.get("warnings", [])],
        }

    def pattern(self, pattern_id: str) -> dict[str, Any]:
        payload = self.repository.pattern(pattern_id)
        if payload is None:
            raise TrendServiceError("Trend pattern was not found.")
        return payload

    def _selected_video_rows(self, snapshot_id: str, video_ids: list[str]) -> list[dict[str, str]]:
        if not video_ids:
            return []
        placeholders = ",".join("?" for _ in video_ids)
        rows = self.database.query(
            "SELECT v.video_id,v.hashtag_id FROM trend_videos v JOIN trend_hashtags h ON h.snapshot_id=v.snapshot_id AND h.hashtag_id=v.hashtag_id "
            f"WHERE v.snapshot_id=? AND v.media_type='video' AND v.is_available=1 "
            f"AND v.video_id IN ({placeholders}) ORDER BY h.rank_position,v.provider_ordinal",
            (snapshot_id, *video_ids),
        )
        selected: dict[str, dict[str, str]] = {}
        for row in rows:
            selected.setdefault(str(row["video_id"]), {"video_id": str(row["video_id"]), "hashtag_id": str(row["hashtag_id"])})
        return [selected[video_id] for video_id in video_ids if video_id in selected]


def _aggregate_pattern(snapshot_id: str, fingerprints: list[dict[str, Any]], cfg: Any) -> dict[str, Any]:
    from variation_profile import load_active_profile, profile_revision

    active = load_active_profile(cfg)
    base_revision = str(active.get("revision") or "")
    base_variant = deepcopy((active.get("variants") or [{}])[0])
    recommendations: dict[str, dict[str, Any]] = {}
    for field in sorted(SUGGESTED_PROFILE_FIELDS):
        entries = []
        for fingerprint in fingerprints:
            item = (fingerprint.get("recommendations") or {}).get(field)
            if isinstance(item, dict) and item.get("value") is not None:
                entries.append((item.get("value"), float(item.get("confidence") or 0.0), fingerprint.get("fingerprint_id")))
        if not entries:
            continue
        value, supports = _aggregate_field(entries)
        support_count = len(supports)
        confidence = statistics.mean(item[1] for item in supports) * support_count / len(fingerprints)
        record = {
            "value": value,
            "support_count": support_count,
            "sample_count": len(fingerprints),
            "confidence": round(confidence, 4),
            "source_fingerprint_ids": [item[2] for item in supports if item[2]],
            "applied_to_suggestion": support_count >= 3 and confidence >= 0.65,
        }
        recommendations[field] = record
        if record["applied_to_suggestion"]:
            base_variant[field] = value
    suggested = {
        "schema_version": int(active.get("schema_version") or 1),
        "variant_count": 1,
        "updated_at": utc_now(),
        "name": f"TikTok trend {snapshot_id[:8]}",
        "variants": [base_variant],
    }
    suggested["revision"] = profile_revision(suggested)
    return {
        "schema_version": 1,
        "analyzer_version": ANALYZER_VERSION,
        "base_profile_revision": base_revision,
        "sample_count": len(fingerprints),
        "fingerprint_ids": [item.get("fingerprint_id") for item in fingerprints],
        "recommendations": recommendations,
        "suggested_profile": suggested,
    }


def _weighted_mode(entries: list[tuple[Any, float, Any]]) -> tuple[Any, list[tuple[Any, float, Any]]]:
    groups: dict[str, list[tuple[Any, float, Any]]] = {}
    for entry in entries:
        groups.setdefault(json.dumps(entry[0], sort_keys=True), []).append(entry)
    winning = max(groups.values(), key=lambda items: (sum(item[1] for item in items), len(items), str(items[0][0])))
    return winning[0][0], winning


def _aggregate_field(entries: list[tuple[Any, float, Any]]) -> tuple[Any, list[tuple[Any, float, Any]]]:
    if entries and all(isinstance(item[0], (int, float)) and not isinstance(item[0], bool) for item in entries):
        return round(float(statistics.median(float(item[0]) for item in entries)), 4), entries
    return _weighted_mode(entries)


def _payload_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    for key in ("list", "trending_list", "hashtag_list"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _payload_pagination(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {"supported": False, "provided": False}
    values = {
        key: data[key]
        for key in ("page_info", "pagination", "cursor", "next_cursor", "has_more")
        if key in data
    }
    return {"supported": False, "provided": bool(values), "values": values}


def _enrich_ranked_video_groups(client: Any, groups: list[dict[str, Any]], *, concurrency: int) -> None:
    candidates: list[dict[str, Any]] = []
    for group in groups:
        for candidate in group.get("top_video_list") or []:
            if not isinstance(candidate, dict) or candidate.get("_post_metadata") is not None:
                continue
            if any(key in candidate for key in ("image_post_info", "video_info", "media_type", "post_type")):
                continue
            candidates.append(candidate)
    if not candidates:
        return
    metadata_fetcher = getattr(client, "post_metadata", None)
    if not callable(metadata_fetcher):
        for candidate in candidates:
            candidate["_post_metadata"] = {"_probe_error": "TikTok player metadata fetcher is unavailable."}
        return

    def fetch(candidate: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        video_id = str(candidate.get("video_id") or "")
        try:
            payload = metadata_fetcher(video_id)
            if not isinstance(payload, dict):
                raise TikTokDiscoveryError("TikTok player metadata returned an invalid payload.")
            return candidate, payload
        except Exception as exc:
            return candidate, {"_probe_error": _sanitize_post_probe_error(exc)}

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = [executor.submit(fetch, candidate) for candidate in candidates]
        for future in as_completed(futures):
            candidate, payload = future.result()
            candidate["_post_metadata"] = payload


def _classify_tiktok_post(candidate: dict[str, Any]) -> dict[str, Any]:
    video_id = str(candidate.get("video_id") or candidate.get("id_str") or candidate.get("id") or "")
    metadata = candidate.get("_post_metadata")
    if not isinstance(metadata, dict):
        metadata = candidate
    if metadata.get("_probe_error"):
        return _post_classification(
            media_type="unknown",
            classification_evidence=f"metadata probe failed: {metadata['_probe_error']}",
            availability_evidence="No reliable TikTok media record was available.",
            exclusion_reason="unknown",
        )

    item = metadata
    if any(key in metadata for key in ("items", "results", "status_code", "extra")):
        status_code = _optional_int(metadata.get("status_code"))
        if status_code not in {None, 0}:
            return _post_classification(
                media_type="unknown",
                classification_evidence=f"player status_code={status_code}",
                availability_evidence=str(metadata.get("status_msg") or "TikTok rejected the player metadata request."),
                exclusion_reason="unavailable",
            )
        fatal_ids = {
            str(value)
            for value in ((metadata.get("extra") or {}).get("fatal_item_ids") or [])
        }
        results = [
            value for value in (metadata.get("results") or [])
            if isinstance(value, dict)
        ]
        matching_result = next(
            (
                value for value in results
                if str(value.get("id_str") or value.get("id") or "") == video_id
            ),
            results[0] if len(results) == 1 else None,
        )
        result_code = str((matching_result or {}).get("code") or "")
        if video_id in fatal_ids or (result_code and result_code.casefold() != "ok"):
            return _post_classification(
                media_type="unknown",
                classification_evidence=f"player result code={result_code or 'fatal_item'}",
                availability_evidence="TikTok marked the post unavailable or returned no core data.",
                exclusion_reason="unavailable",
            )
        items = [value for value in (metadata.get("items") or []) if isinstance(value, dict)]
        item = next(
            (
                value for value in items
                if str(value.get("id_str") or value.get("id") or "") == video_id
            ),
            items[0] if len(items) == 1 else {},
        )
        if not item:
            return _post_classification(
                media_type="unknown",
                classification_evidence="player response contained no matching item",
                availability_evidence="TikTok returned no playable post record.",
                exclusion_reason="unavailable",
            )

    aweme_type = _optional_int(item.get("aweme_type"))
    image_info = item.get("image_post_info")
    if isinstance(image_info, dict):
        images = [value for value in (image_info.get("images") or []) if isinstance(value, dict)]
        image_count = len(images)
        media_type = "carousel" if image_count > 1 else "image"
        return _post_classification(
            media_type=media_type,
            classification_evidence=(
                f"player item.image_post_info.images count={image_count}; "
                f"aweme_type={aweme_type if aweme_type is not None else 'not_provided'}"
            ),
            availability_evidence="Image posts do not contain playable video media.",
            image_count=image_count,
            provider_aweme_type=aweme_type,
            exclusion_reason="image_or_carousel",
        )
    if aweme_type == 150:
        return _post_classification(
            media_type="image",
            classification_evidence="player aweme_type=150 (image post); image list was not provided",
            availability_evidence="TikTok identified an image post, not video media.",
            provider_aweme_type=aweme_type,
            exclusion_reason="image_or_carousel",
        )

    video_info = item.get("video_info")
    if isinstance(video_info, dict):
        meta = video_info.get("meta") if isinstance(video_info.get("meta"), dict) else {}
        duration = _optional_float(meta.get("duration"))
        urls = video_info.get("url_list")
        if not isinstance(urls, list):
            play_addr = video_info.get("play_addr")
            urls = play_addr.get("url_list") if isinstance(play_addr, dict) else []
        playable_url_count = sum(1 for value in (urls or []) if _is_https_url(value))
        format_name = str(meta.get("format") or "").strip()
        evidence = (
            "player item.video_info present; "
            f"meta.duration={duration if duration is not None else 'not_provided'}; "
            f"meta.format={format_name or 'not_provided'}; "
            f"nonempty_playable_urls={playable_url_count}; "
            f"aweme_type={aweme_type if aweme_type is not None else 'not_provided'}"
        )
        if duration is not None and duration > 0 and playable_url_count > 0:
            return _post_classification(
                media_type="video",
                is_available=True,
                classification_evidence=evidence,
                availability_evidence="Fresh TikTok player metadata contains playable video data.",
                video_duration_seconds=duration,
                playable_url_count=playable_url_count,
                provider_aweme_type=aweme_type,
            )
        return _post_classification(
            media_type="video",
            classification_evidence=evidence,
            availability_evidence="Video metadata exists, but TikTok supplied no playable video URL or positive duration.",
            video_duration_seconds=duration,
            playable_url_count=playable_url_count,
            provider_aweme_type=aweme_type,
            exclusion_reason="unavailable",
        )

    explicit_type = str(item.get("media_type") or item.get("post_type") or item.get("item_type") or "").casefold()
    if explicit_type in {
        "image", "image_post", "photo", "photo_post", "photo_mode", "carousel", "slideshow",
    }:
        normalized = "carousel" if explicit_type in {"carousel", "slideshow", "photo_mode"} else "image"
        return _post_classification(
            media_type=normalized,
            classification_evidence=f"explicit provider media type={explicit_type}",
            availability_evidence="Provider identified a non-video post.",
            provider_aweme_type=aweme_type,
            exclusion_reason="image_or_carousel",
        )
    return _post_classification(
        media_type="unknown",
        classification_evidence=(
            "No image_post_info, video_info, or recognized explicit provider media type was present."
        ),
        availability_evidence="Unknown posts are excluded unless reliable playable-video metadata exists.",
        provider_aweme_type=aweme_type,
        exclusion_reason="unknown",
    )


def _post_classification(
    *,
    media_type: str,
    classification_evidence: str,
    availability_evidence: str,
    is_available: bool = False,
    video_duration_seconds: float | None = None,
    image_count: int | None = None,
    playable_url_count: int = 0,
    provider_aweme_type: int | None = None,
    exclusion_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "media_type": media_type,
        "is_available": bool(is_available and media_type == "video"),
        "classification_evidence": classification_evidence[:1000],
        "availability_evidence": availability_evidence[:1000],
        "video_duration_seconds": video_duration_seconds,
        "image_count": image_count,
        "playable_url_count": int(playable_url_count),
        "provider_aweme_type": provider_aweme_type,
        "exclusion_reason": exclusion_reason,
    }


def _first_ranked_videos_by_hashtag(
    rows: Iterable[dict[str, Any]],
    *,
    limit: int = TIKTOK_RANKED_VIDEO_LIMIT,
) -> list[dict[str, Any]]:
    seen: dict[str, set[str]] = {}
    selected: list[dict[str, Any]] = []
    for row in rows:
        hashtag_id = str(row.get("hashtag_id") or "")
        video_id = str(row.get("video_id") or "")
        final_rank = int(row.get("final_rank") or 0)
        if not hashtag_id or not video_id or video_id in seen.setdefault(hashtag_id, set()):
            continue
        if not 1 <= final_rank <= limit:
            continue
        seen[hashtag_id].add(video_id)
        selected.append(row)
    return selected


def normalize_hashtag_folder_name(hashtag: Any) -> str:
    """Return one lower-case Windows-safe path segment without changing valid characters."""
    normalized = str(hashtag or "").strip()
    if normalized.startswith("#"):
        normalized = normalized[1:].strip()
    normalized = WINDOWS_INVALID_FOLDER_CHARS.sub("_", normalized).lower().rstrip(" .")
    if normalized in {"", ".", ".."}:
        return "unknown_hashtag"
    if WINDOWS_RESERVED_FOLDER_NAMES.match(normalized):
        normalized = f"hashtag_{normalized}"
    return normalized


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def trend_download_filename(rank: int, video_id: Any) -> str:
    normalized_video_id = str(video_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", normalized_video_id):
        raise TrendServiceError("TikTok video ID is not safe for local storage.")
    normalized_rank = int(rank)
    if not 1 <= normalized_rank <= TIKTOK_RANKED_VIDEO_LIMIT:
        raise TrendServiceError("TikTok video rank must be between 1 and 20.")
    return f"{normalized_rank:03d}_{normalized_video_id}.mp4"


def trend_download_relative_path(hashtag: Any, rank: int, video_id: Any) -> str:
    return (
        Path("downloads")
        / normalize_hashtag_folder_name(hashtag)
        / trend_download_filename(rank, video_id)
    ).as_posix()


def _find_existing_ranked_video(
    hashtag_dir: Path,
    expected_name: str,
    video_id: str,
) -> Path | None:
    if not hashtag_dir.is_dir():
        return None
    candidates = [hashtag_dir / expected_name]
    candidates.extend(
        path
        for path in sorted(hashtag_dir.glob(f"???_{video_id}.mp4"))
        if path.name != expected_name
    )
    for candidate in candidates:
        try:
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                continue
            _probe_downloaded_video(candidate)
            return candidate
        except (OSError, TrendServiceError):
            continue
    return None


def _find_legacy_video(downloads_root: Path, video_id: str) -> Path | None:
    legacy_file = downloads_root / video_id / f"{video_id}.mp4"
    try:
        if not legacy_file.is_file() or legacy_file.stat().st_size <= 0:
            return None
        _probe_downloaded_video(legacy_file)
        return legacy_file
    except (OSError, TrendServiceError):
        return None


def _write_hashtag_metadata(
    stage_dir: Path,
    hashtag_name: str,
    normalized_hashtag: str,
    rows: Iterable[dict[str, Any]],
) -> None:
    videos = []
    for row in rows:
        rank = int(row["final_rank"])
        video_id = str(row["video_id"])
        filename = trend_download_filename(rank, video_id)
        payload = _json_object(row.get("payload_json"))
        author_value = payload.get("author")
        if isinstance(author_value, dict):
            author = str(
                author_value.get("unique_id")
                or author_value.get("nickname")
                or author_value.get("display_name")
                or ""
            )
        else:
            author = str(author_value or payload.get("author_name") or "")
        statistics_payload = payload.get("statistics")
        if not isinstance(statistics_payload, dict):
            statistics_payload = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        view_count = _optional_int(
            row.get("view_count")
            if row.get("view_count") is not None
            else statistics_payload.get("play_count", statistics_payload.get("view_count"))
        )
        videos.append({
            "rank": rank,
            "video_id": video_id,
            "filename": filename,
            "relative_path": (Path(normalized_hashtag) / filename).as_posix(),
            "share_url": str(row.get("share_url") or ""),
            "author": author,
            "view_count": view_count,
        })
    metadata = {
        "hashtag": normalized_hashtag,
        "source_hashtag": str(hashtag_name or ""),
        "refreshed_at": utc_now(),
        "videos": videos,
    }
    temporary_path = stage_dir / f".metadata.{uuid4().hex}.tmp"
    with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, stage_dir / "metadata.json")


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _replace_hashtag_directory(final_dir: Path, stage_dir: Path) -> Path | None:
    backup_dir = final_dir.parent / f".{final_dir.name}.{uuid4().hex}.backup"
    had_previous = final_dir.exists()
    if had_previous:
        os.replace(final_dir, backup_dir)
    try:
        os.replace(stage_dir, final_dir)
    except BaseException:
        if had_previous and backup_dir.exists() and not final_dir.exists():
            os.replace(backup_dir, final_dir)
        raise
    return backup_dir if had_previous else None


def _restore_hashtag_directory(final_dir: Path, backup_dir: Path | None) -> None:
    if final_dir.exists():
        shutil.rmtree(final_dir)
    if backup_dir is not None and backup_dir.exists():
        os.replace(backup_dir, final_dir)


def _video_diagnostics_for_frontend(
    hashtags: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    sent_videos: list[dict[str, Any]],
    *,
    pagination_available: bool,
) -> list[dict[str, Any]]:
    candidates_by_hashtag: dict[str, list[dict[str, Any]]] = {}
    sent_by_hashtag: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        candidates_by_hashtag.setdefault(str(row.get("hashtag_id") or ""), []).append(row)
    for row in sent_videos:
        sent_by_hashtag.setdefault(str(row.get("hashtag_id") or ""), []).append(row)
    diagnostics: list[dict[str, Any]] = []
    for hashtag in hashtags:
        hashtag_id = str(hashtag.get("hashtag_id") or "")
        rows = candidates_by_hashtag.get(hashtag_id, [])
        sent = sent_by_hashtag.get(hashtag_id, [])
        diagnostics.append({
            "hashtag_id": hashtag_id,
            "hashtag_name": str(hashtag.get("hashtag_name") or ""),
            "total_candidates_returned": len(rows),
            "video_posts_detected": sum(row.get("media_type") == "video" for row in rows),
            "image_carousel_posts_excluded": sum(
                row.get("exclusion_reason") == "image_or_carousel" for row in rows
            ),
            "unknown_posts_excluded": sum(row.get("exclusion_reason") == "unknown" for row in rows),
            "unavailable_posts_excluded": sum(
                row.get("exclusion_reason") == "unavailable" for row in rows
            ),
            "valid_videos_stored": sum(
                row.get("media_type") == "video" and bool(row.get("is_available"))
                for row in rows
            ),
            "sent_to_frontend": len(sent),
            "pagination_available": pagination_available,
            "candidate_limit": TIKTOK_RANKED_VIDEO_LIMIT,
            "endpoint": "/open_api/v1.3/discovery/video_list/",
            "candidates": [
                {
                    "video_id": str(row.get("video_id") or ""),
                    "original_tiktok_rank": int(row.get("provider_ordinal") or 0),
                    "final_rank": _optional_int(row.get("final_rank")),
                    "media_type": str(row.get("media_type") or "unknown"),
                    "is_available": bool(row.get("is_available")),
                    "classification_evidence": str(row.get("classification_evidence") or ""),
                    "availability_evidence": str(row.get("availability_evidence") or ""),
                    "exclusion_reason": row.get("exclusion_reason"),
                }
                for row in rows
            ],
        })
    return diagnostics


def _is_https_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.casefold() == "https" and bool(parsed.hostname)


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sanitize_post_probe_error(exc: Exception) -> str:
    message = re.sub(r"https?://\S+", "[url]", str(exc or "metadata probe failed"), flags=re.IGNORECASE)
    message = re.sub(r"\s+", " ", message).strip()
    return f"{type(exc).__name__}: {message}"[:500]


def _replace_payload_list(payload: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Copy a provider response while replacing only its discovered hashtag list."""
    filtered = deepcopy(payload)
    data = filtered.get("data")
    if not isinstance(data, dict):
        data = {}
        filtered["data"] = data
    for key in ("list", "trending_list", "hashtag_list"):
        if isinstance(data.get(key), list):
            data[key] = items
            break
    else:
        data["list"] = items
    return filtered


def _country_code(value: Any) -> str:
    country = str(value or "ID").strip().upper()
    if not re_fullmatch_country(country):
        raise TrendServiceError("country_code must be a two-letter ISO code")
    return country


def re_fullmatch_country(value: str) -> bool:
    return len(value) == 2 and value.isalpha() and value.isascii()


def _validate_video(path: Path) -> None:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise TrendServiceError("Selected trend media is not a readable video.")
    try:
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except json.JSONDecodeError as exc:
        raise TrendServiceError("Selected trend media returned invalid probe data.") from exc
    if not streams:
        raise TrendServiceError("Selected trend media has no video stream.")


def _validated_tiktok_video_url(value: str, video_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", video_id):
        raise TrendServiceError("TikTok video ID is not safe for local storage.")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise TrendServiceError("TikTok video URL is malformed.") from exc
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or not (hostname == "tiktok.com" or hostname.endswith(".tiktok.com")):
        raise TrendServiceError("TikTok video URL must use HTTPS on a TikTok host.")
    match = re.search(r"/video/([A-Za-z0-9_-]+)(?:/|$)", parsed.path)
    if match is None or match.group(1) != video_id:
        raise TrendServiceError("TikTok video URL does not match the persisted video ID.")
    return urllib.parse.urlunsplit(("https", hostname, parsed.path, "", ""))


def _validated_tiktok_media_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError as exc:
        raise TrendServiceError("TikTok player metadata returned a malformed media URL.") from exc
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    allowed = any(
        hostname == suffix.removeprefix(".") or hostname.endswith(suffix)
        for suffix in TIKTOK_MEDIA_HOST_SUFFIXES
    )
    if parsed.scheme.casefold() != "https" or not allowed:
        raise TrendServiceError("TikTok player metadata returned a media URL outside approved TikTok CDN hosts.")
    return str(value)


def _resolve_provider_media_url(video_id: str) -> str:
    payload = TikTokDiscoveryClient("", "").post_metadata(video_id)
    items = [value for value in (payload.get("items") or []) if isinstance(value, dict)]
    item = next(
        (
            value for value in items
            if str(value.get("id_str") or value.get("id") or "") == video_id
        ),
        items[0] if len(items) == 1 else {},
    )
    video_info = item.get("video_info") if isinstance(item, dict) else None
    if not isinstance(video_info, dict):
        raise TrendServiceError("Video is unavailable: TikTok returned no playable video metadata.")
    urls = video_info.get("url_list")
    if not isinstance(urls, list):
        play_addr = video_info.get("play_addr")
        urls = play_addr.get("url_list") if isinstance(play_addr, dict) else []
    for value in urls or []:
        if isinstance(value, str) and value.strip():
            try:
                return _validated_tiktok_media_url(value.strip())
            except TrendServiceError:
                continue
    raise TrendServiceError("Video is unavailable: TikTok returned no approved playable media URL.")


def _run_ytdlp(video_id: str, source_url: str, target_dir: Path, timeout: int) -> Path:
    source_url = _validated_tiktok_video_url(source_url, video_id)
    media_url = _resolve_provider_media_url(video_id)
    output_template = str(target_dir / f"{video_id}.%(ext)s")
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-overwrites",
        "--no-progress",
        "--no-colors",
        "--restrict-filenames",
        "--socket-timeout",
        "30",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
        "--add-header",
        "User-Agent:Mozilla/5.0",
        "--add-header",
        f"Referer:https://www.tiktok.com/player/v1/{video_id}",
        "--merge-output-format",
        "mp4",
        "-f",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "-o",
        output_template,
        media_url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise TrendServiceError(f"yt-dlp exceeded the {timeout}-second per-video timeout.") from exc
    except OSError as exc:
        raise TrendServiceError("yt-dlp could not be started.") from exc
    if result.returncode != 0:
        raise TrendServiceError(_sanitize_download_error(
            result.stderr or "yt-dlp failed to download the TikTok provider media."
        ))
    candidates = sorted(
        (
            path for path in target_dir.glob(f"{video_id}.*")
            if path.is_file() and path.suffix.casefold() in SUPPORTED_TREND_MEDIA_EXTENSIONS
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    if not candidates:
        raise TrendServiceError("yt-dlp completed without producing a supported video file.")
    return candidates[0]


def _classify_download_error(error: str) -> DownloadFailure:
    raw = _sanitize_download_error(error)
    message = raw.casefold()
    if "unexpected webpage response" in message or "unexpected response from webpage request" in message:
        return DownloadFailure(
            "tiktok_challenge", "tiktok_challenge_unexpected_response",
            "TikTok extractor returned an unexpected webpage response.", True,
        )
    if "universal data for rehydration" in message or "could not read the current webpage" in message:
        return DownloadFailure(
            "tiktok_challenge", "tiktok_challenge_rehydration",
            "TikTok extractor could not read the current webpage response.", True,
        )
    if any(value in message for value in ("http error 429", "too many requests", "rate limit")):
        return DownloadFailure(
            "rate_limit", "tiktok_rate_limit",
            "TikTok rate-limited the downloader.", True,
        )
    if any(value in message for value in ("http error 403", "forbidden", "challenge cookie")):
        return DownloadFailure(
            "tiktok_challenge", "tiktok_access_challenge",
            "TikTok rejected the configured download request.", True,
        )
    if any(value in message for value in ("connection timed out", "timed out", "timeout")):
        return DownloadFailure(
            "network", "network_timeout",
            "TikTok request timed out while contacting the provider.", True,
        )
    if any(value in message for value in ("tls connect", "ssl", "certificate")):
        return DownloadFailure(
            "network", "network_tls",
            "The downloader could not establish a secure connection to TikTok.", True,
        )
    if any(value in message for value in ("proxy", "could not resolve", "name resolution", "network is unreachable")):
        return DownloadFailure(
            "network", "network_connectivity",
            "The downloader could not reach TikTok through the configured network.", True,
        )
    if any(value in message for value in (
        "video is unavailable", "private video", "deleted video", "http error 404", "not available",
    )):
        return DownloadFailure("unavailable", "individual_unavailable", "Video is unavailable.", False)
    if any(value in message for value in (
        "ffprobe", "invalid duration", "must contain", "video stream", "mp4 container", "corrupt",
    )):
        return DownloadFailure("validation", "media_validation", raw, False)
    if any(value in message for value in (
        "yt-dlp could not be started", "completed without producing", "no supported video file",
        "impersonate target", "extractor failed",
    )):
        return DownloadFailure(
            "extractor", "downloader_configuration",
            "The configured TikTok downloader could not produce a video.", True,
        )
    return DownloadFailure("individual", "individual_download_failure", raw, False)


def _probe_downloaded_video(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise TrendServiceError("Downloaded media failed ffprobe validation.")
    try:
        payload = json.loads(result.stdout or "{}")
        stream_types = {str(item.get("codec_type") or "") for item in payload.get("streams") or []}
        format_payload = payload.get("format") or {}
        duration = float(format_payload.get("duration") or 0.0)
        format_names = {
            value.strip().casefold()
            for value in str(format_payload.get("format_name") or "").split(",")
            if value.strip()
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TrendServiceError("Downloaded media returned invalid ffprobe data.") from exc
    if "video" not in stream_types:
        raise TrendServiceError("Downloaded media must contain a supported video stream.")
    if "mp4" not in format_names:
        raise TrendServiceError("Downloaded media is not an MP4 container.")
    if duration <= 0:
        raise TrendServiceError("Downloaded media has an invalid duration.")
    return {
        "duration_seconds": duration,
        "stream_types": sorted(stream_types),
        "format_names": sorted(format_names),
    }


def _free_disk_bytes(path: Path) -> int:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        return int(shutil.disk_usage(candidate).free)
    except OSError:
        return 0


def _sanitize_download_error(message: str) -> str:
    sanitized = str(message or "Download failed.").replace("\r", " ").replace("\n", " ")
    lowered = sanitized.casefold()
    if "unexpected response from webpage request" in lowered:
        return "TikTok extractor returned an unexpected webpage response."
    if "unable to extract universal data for rehydration" in lowered:
        return "TikTok extractor could not read the current webpage response."
    sanitized = re.sub(r"https?://\S+", "[url]", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(
        r"(?i)authorization\s*[:=]\s*(?:bearer\s+)?\S+",
        "authorization=[redacted]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)(cookie|access[-_ ]?token|refresh[-_ ]?token|secret|signature|session(?:id)?)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        sanitized,
    )
    sanitized = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+\-/]+=*", "Bearer [redacted]", sanitized)
    for secret in (
        os.getenv("TIKTOK_APP_SECRET", "").strip(),
        os.getenv("TIKTOK_ACCESS_TOKEN", "").strip(),
    ):
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    return re.sub(r"\s+", " ", sanitized).strip()[:1000]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_provider_message(message: str, access_token: str) -> str:
    sanitized = str(message or "")
    secrets = (
        access_token,
        os.getenv("TIKTOK_APP_SECRET", "").strip(),
        os.getenv("TIKTOK_ACCESS_TOKEN", "").strip(),
    )
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    return sanitized[:1000]


def _discovery_transport_error(exc: requests.RequestException, timeout: float) -> str:
    if isinstance(exc, requests.exceptions.Timeout):
        return f"TikTok Discovery timed out after {timeout:g} seconds. Check the network or proxy and try again."
    if isinstance(exc, requests.exceptions.ProxyError):
        return "TikTok Discovery could not connect through the configured HTTPS proxy. Check the proxy and try again."
    if isinstance(exc, requests.exceptions.SSLError):
        return "TikTok Discovery could not establish a secure TLS connection. Check the HTTPS proxy, certificate trust, and system clock."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "TikTok Discovery could not connect. Check DNS, VPN, firewall, and HTTPS proxy settings."
    return "TikTok Discovery request failed before a response was received. Check the network and try again."


def _provider_auth_rejected(payload: dict[str, Any]) -> bool:
    if payload.get("code") in {0, "0", None}:
        return False
    message = str(payload.get("message") or "").casefold()
    return "access token" in message and any(word in message for word in ("invalid", "expired", "revoked", "unauthorized"))


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
