from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app" / "static" / "onair" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "app" / "static" / "onair" / "app.js").read_text(encoding="utf-8")


def test_show_lifecycle_is_operator_accessible_without_scripts():
    for control in (
        "showForm",
        "showSelect",
        "deleteShowButton",
        "showAssignmentForm",
        "showAudioForm",
        "showGoLiveButton",
        "showGoBreakButton",
        "showEndButton",
    ):
        assert f'id="{control}"' in HTML
    for endpoint in (
        "/api/shows/?station_id=",
        "/api/shows/session/current?station_id=",
        "/assignments",
        "/assignment-candidates",
        "/assign/",
        "/upload-audio",
        "/go-live",
        "/go-break",
        "/end",
    ):
        assert endpoint in JS
    assert "showDeleteArmedUntil" in JS
    assert "Show read-back did not match the saved configuration" in JS
    assert "Assignment read-back did not contain the selected operator" in JS


def test_permanent_music_usage_and_monthly_close_are_operator_accessible():
    for control in (
        "musicUsageFilterForm",
        "exportMusicUsageButton",
        "musicMetadataForm",
        "musicMonthlyCloseForm",
        "musicUsageList",
        "musicClosureList",
    ):
        assert f'id="{control}"' in HTML
    for endpoint in (
        "/api/music-usage?",
        "/api/music-usage/export?",
        "/api/music-usage/track-metadata/",
        "/api/music-usage/monthly-close",
        "/api/music-usage/monthly-closures",
    ):
        assert endpoint in JS
    assert "Download CSV for Excel" in HTML
    assert "append-only and hash chained" in HTML
    assert "Metadata read-back did not match the saved values" in JS
    assert "idempotent: true" in JS


def test_streaming_origin_operations_are_safe_and_operator_accessible():
    for control in (
        "streamingFeaturesForm",
        "refreshStreamingHealthButton",
        "streamingManagementForm",
        "moveStreamingListenersButton",
        "kickStreamingSourceButton",
        "insertStreamingMidrollButton",
    ):
        assert f'id="{control}"' in HTML
    for endpoint in (
        "/api/streaming/features",
        "/api/streaming/health",
        "/api/streaming/manage/move-listeners",
        "/api/streaming/manage/kick",
        "/api/streaming/manage/midroll",
    ):
        assert endpoint in JS
    assert "streamingActionArmed" in JS
    assert "Date.now() + 20000" in JS
    assert "assertOriginManagementAccepted" in JS
    assert "Metadata suppression remains enforced" in HTML
    assert "/api/streaming/metadata" not in JS


def test_advertising_items_break_sets_and_campaigns_are_operator_accessible():
    for control in (
        "adItemForm",
        "adRuntimeList",
        "adBreakSetForm",
        "deleteAdBreakSetButton",
        "adCampaignForm",
        "deleteAdCampaignButton",
    ):
        assert f'id="{control}"' in HTML
    for endpoint in (
        "/api/ads/items",
        "/api/ads/runtime",
        "/api/ad-break-sets",
        "/api/ad-campaigns",
    ):
        assert endpoint in JS
    assert "Break-set read-back did not match the saved values" in JS
    assert "Campaign read-back did not match the saved values" in JS
    assert "adDeleteArmed" in JS


def test_guest_recording_archive_is_operator_accessible_and_guarded():
    for control in (
        "guestRecordingLibraryPanel",
        "refreshGuestRecordingsButton",
        "guestRecordingList",
    ):
        assert f'id="{control}"' in HTML
    for endpoint in (
        "/api/guest-recordings?station_id=",
        "/api/guest-recordings/${id}/download",
        "/api/guest-recordings/${id}",
    ):
        assert endpoint in JS
    assert "guestRecordingDeleteArmed" in JS
    assert "Deleted guest recording still appeared in read-back" in JS


def test_playlist_catalog_and_full_editor_are_operator_accessible():
    for control in (
        "playlistCreateForm",
        "playlistAutoForm",
        "playlistSelect",
        "playlistAddItemForm",
        "playlistBulkForm",
        "deletePlaylistButton",
        "playlistItemList",
    ):
        assert f'id="{control}"' in HTML
    for endpoint in (
        "/api/playlists?station_id=",
        "/api/playlists/auto/generate",
        "/items",
        "/bulk",
        "/reorder",
    ):
        assert endpoint in JS
    assert "Playlist order did not match read-back" in JS
    assert "playlistDeleteArmedUntil" in JS


def test_speaker_monitor_and_startup_sound_are_operator_accessible():
    for control in (
        "speakerMonitorForm",
        "speakerMonitorStation",
        "startupSoundForm",
        "startupSoundUploadForm",
        "startupSoundTrack",
    ):
        assert f'id="{control}"' in HTML
    for endpoint in (
        "/api/speaker/monitor",
        "/api/startup-sound/config",
        "/api/startup-sound/upload",
    ):
        assert endpoint in JS
    assert "Speaker monitor read-back did not match" in JS
    assert "Startup-sound read-back did not match" in JS
    assert "Uploaded startup audio was missing from read-back" in JS


def test_download_queue_and_library_metadata_tools_are_operator_accessible():
    for control in (
        "ytdlpImportForm",
        "ytdlpJobList",
        "metadataRuleForm",
        "metadataRuleList",
        "metadataMaintenanceForm",
    ):
        assert f'id="{control}"' in HTML
    for endpoint in (
        "/api/library/import/ytdlp/settings",
        "/api/library/import/ytdlp/jobs",
        "/api/library/import/ytdlp/jobs/status",
        "/api/library/metadata/rules",
        "/api/library/metadata/normalize",
        "/api/library/metadata/autofix",
        "/api/library/metadata/verify/itunes",
        "/api/library/bpm/analyze",
    ):
        assert endpoint in JS
    assert "Download job was queued but missing from read-back" in JS
    assert "metadataMaintenanceArmedUntil" in JS
    assert "public stream metadata" in HTML


def test_new_operator_controls_have_unique_dom_ids_and_static_references_resolve():
    import re

    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids))
    referenced = set(re.findall(r"\$\('([^']+)'\)", JS))
    assert referenced <= set(ids)
