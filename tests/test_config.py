import pytest
from pydantic import ValidationError

from pwcs2_bot.channel import PWCS2Config


@pytest.mark.parametrize(
    "targets",
    [
        [
            {"platform": "huya", "name": "A", "roomId": "10188"},
            {"platform": "huya", "name": "B", "roomId": "10188"},
        ],
        [
            {
                "platform": "douyin",
                "name": "A",
                "roomId": "not-a-room",
            }
        ],
        [
            {
                "platform": "douyu",
                "name": "A",
                "roomId": "6979222",
                "url": "https://evil.example/6979222",
            }
        ],
    ],
)
def test_plugin_config_rejects_invalid_live_targets(targets) -> None:
    with pytest.raises(ValidationError):
        PWCS2Config.model_validate({"streamLiveTargets": targets})


def test_plugin_config_accepts_nanobot_camel_case_keys(tmp_path) -> None:
    config = PWCS2Config.model_validate(
        {
            "enabled": True,
            "wsUrl": "ws://napcat:3001",
            "accessToken": "onebot-secret",
            "allowFrom": ["*"],
            "pwToken": "perfect-secret",
            "pwMySteamId": "76561198000000000",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["123"],
            "streamLiveEnabled": True,
            "streamLiveTargets": [
                {
                    "platform": "douyu",
                    "name": "玩机器",
                    "roomId": "6979222",
                    "url": "https://www.douyu.com/6657",
                },
                {
                    "platform": "huya",
                    "name": "DANK1NG",
                    "roomId": "10188",
                    "url": "https://www.huya.com/dank1ng",
                },
                {
                    "platform": "douyin",
                    "name": "MasTurbaT0r",
                    "roomId": "59901412180",
                    "url": "https://live.douyin.com/59901412180",
                    "expectedSecUid": "MS4wLjABAAAA-test",
                },
            ],
            "streamLivePollIntervalSeconds": 90,
            "streamLiveInitialDelaySeconds": 15,
            "streamLiveTimeoutSeconds": 8,
            "streamLiveConfirmationPolls": 3,
            "privateRepliesEnabled": False,
            "xhReactionEnabled": False,
            "trainingGameEnabled": True,
            "trainingGameUrl": "https://game.example.test/cs2-training.html",
            "trainingGameGroupCooldownSeconds": 900,
            "trainingGameUserCooldownSeconds": 45,
            "trainingGameFileUploadEnabled": False,
            "statsImageEnabled": True,
            "statsImageTimeoutSeconds": 20,
            "valorantEnabled": True,
            "valorantQueryUrl": "https://wgval.cloud/public-query",
            "valorantTimeoutSeconds": 25,
            "valorantQueueTimeoutSeconds": 4,
            "valorantCacheTtlSeconds": 900,
            "valorantStaleTtlSeconds": 90000,
            "valorantFailureThreshold": 4,
            "valorantFailureCooldownSeconds": 1200,
            "matchNotificationsEnabled": True,
            "matchPollIntervalSeconds": 120,
            "matchAiCommentaryEnabled": True,
            "matchAiCommentaryMaxTokens": 600,
            "partyExpirationHours": 8,
            "steamWebApiKey": "steam-secret",
            "steamPresenceBaseUrl": "https://api.steampowered.com",
            "steamPresenceProxyUrl": "http://steam-mihomo:7890",
            "steamPresenceTimeoutSeconds": 15,
            "steamPresenceCacheTtlSeconds": 50,
            "steamPresenceStaleTtlSeconds": 1200,
            "steamPresenceFailureCooldownSeconds": 20,
            "weeklyChemistryEnabled": True,
            "weeklyChemistryTime": "21:30",
            "weeklyChemistryWeekday": 6,
            "weeklyChemistryMatchPageSize": 50,
            "membershipSyncEnabled": True,
            "membershipSyncIntervalSeconds": 3600,
            "groupCardLockEnabled": True,
            "groupCardLockReconcileIntervalSeconds": 240,
            "groupCardLockReconcileInitialDelaySeconds": 12,
            "unboundGuardEnabled": True,
            "unboundDailyMessageLimit": 8,
            "unboundMuteDurationSeconds": 300,
            "dailySummaryEnabled": True,
            "dailySummaryTime": "23:30",
            "dailyWordCloudEnabled": True,
            "dailyWordCloudTime": "23:30",
            "dailyWordCloudMaxWords": 40,
            "adminBanGuardEnabled": True,
            "adminBanRevokeThresholdSeconds": 600,
            "skinPriceEnabled": True,
            "skinPriceTime": "12:00",
            "skinPriceMaxItemsPerGroup": 20,
            "skinPriceLocalizationCacheTtlSeconds": 86400,
            "groupLevelRemovalGraceDays": 15,
            "groupLevelRemovalMaxPerDay": 10,
            "contentRelayEnabled": True,
            "contentRelaySourceGroups": ["456", "789"],
            "contentRelayTargetGroup": "123",
            "contentRelayPrioritySourceGroups": ["789"],
            "contentRelayPriorityCooldownSeconds": 900,
            "contentRelaySecondaryDailyLimit": 3,
            "aiEnabled": True,
            "aiApiKey": "sk-test-secret",
            "aiBaseUrl": "https://api.llm.ustc.edu.cn/v1",
            "aiModel": "deepseek-v4-flash-ascend",
            "aiVisionModel": "qwen3.6-chat",
            "aiVisionMaxTokens": 1200,
            "aiTimeoutSeconds": None,
            "aiMaxTokens": 4096,
            "aiMaxHistoryMessages": 6,
            "aiMaxInputChars": 16000,
            "aiMaxConcurrentRequests": 20,
            "aiGroupSharedContextEnabled": True,
            "aiAllowConcurrentSameChat": True,
            "aiMultimodalEnabled": True,
            "aiMaxImages": 2,
            "aiImageMaxBytes": 4000000,
            "aiImageTimeoutSeconds": 12,
            "aiMemberMemoryEnabled": True,
            "aiMemberMemoryRefreshIntervalSeconds": 21600,
            "aiMemberMemoryMaxMembers": 50,
            "aiMemberMemoryMaxContextChars": 12000,
            "aiGroupMemoryEnabled": True,
            "aiGroupMemoryMaxEntries": 30,
            "aiGroupMemoryMaxEntriesPerMember": 6,
            "aiGroupMemoryMaxContentChars": 240,
            "aiGroupMemoryMaxContextChars": 7000,
            "tsEnabled": True,
            "tsQueryHost": "teamspeak-query",
            "tsQueryPort": 10011,
            "tsPublicAddress": "ts.example.com",
            "forwardUnmatched": False,
        }
    )

    assert config.ws_url == "ws://napcat:3001"
    assert config.pw_token == "perfect-secret"
    assert config.pw_my_steam_id == "76561198000000000"
    assert config.allowed_groups == ["123"]
    assert config.stream_live_enabled is True
    assert [target.room_id for target in config.stream_live_targets] == [
        "6979222",
        "10188",
        "59901412180",
    ]
    assert config.stream_live_targets[2].expected_sec_uid == "MS4wLjABAAAA-test"
    assert config.stream_live_poll_interval_seconds == 90
    assert config.stream_live_initial_delay_seconds == 15
    assert config.stream_live_timeout_seconds == 8
    assert config.stream_live_confirmation_polls == 3
    assert config.private_replies_enabled is False
    assert config.xh_reaction_enabled is False
    assert config.training_game_enabled is True
    assert config.training_game_url == "https://game.example.test/cs2-training.html"
    assert config.training_game_group_cooldown_seconds == 900
    assert config.training_game_user_cooldown_seconds == 45
    assert config.training_game_file_upload_enabled is False
    assert config.stats_image_enabled is True
    assert config.stats_image_timeout_seconds == 20
    assert config.valorant_enabled is True
    assert config.valorant_query_url == "https://wgval.cloud/public-query"
    assert config.valorant_timeout_seconds == 25
    assert config.valorant_queue_timeout_seconds == 4
    assert config.valorant_cache_ttl_seconds == 900
    assert config.valorant_stale_ttl_seconds == 90000
    assert config.valorant_failure_threshold == 4
    assert config.valorant_failure_cooldown_seconds == 1200
    assert config.match_notifications_enabled is True
    assert config.match_poll_interval_seconds == 120
    assert config.match_ai_commentary_enabled is True
    assert config.match_ai_commentary_max_tokens == 600
    assert config.party_expiration_hours == 8
    assert config.steam_web_api_key == "steam-secret"
    assert config.steam_presence_base_url == "https://api.steampowered.com"
    assert config.steam_presence_proxy_url == "http://steam-mihomo:7890"
    assert config.steam_presence_timeout_seconds == 15
    assert config.steam_presence_cache_ttl_seconds == 50
    assert config.steam_presence_stale_ttl_seconds == 1200
    assert config.steam_presence_failure_cooldown_seconds == 20
    assert config.weekly_chemistry_enabled is True
    assert config.weekly_chemistry_time == "21:30"
    assert config.weekly_chemistry_weekday == 6
    assert config.weekly_chemistry_match_page_size == 50
    assert config.membership_sync_enabled is True
    assert config.membership_sync_interval_seconds == 3600
    assert config.group_card_lock_enabled is True
    assert config.group_card_lock_reconcile_interval_seconds == 240
    assert config.group_card_lock_reconcile_initial_delay_seconds == 12
    assert config.unbound_guard_enabled is True
    assert config.unbound_daily_message_limit == 8
    assert config.unbound_mute_duration_seconds == 300
    assert config.daily_summary_enabled is True
    assert config.daily_summary_time == "23:30"
    assert config.daily_admin_enabled is True
    assert config.daily_admin_time == "00:00"
    assert config.admin_ban_guard_enabled is True
    assert config.admin_ban_revoke_threshold_seconds == 600
    assert config.daily_word_cloud_enabled is True
    assert config.daily_word_cloud_time == "23:30"
    assert config.daily_word_cloud_max_words == 40
    assert config.skin_price_enabled is True
    assert config.skin_price_time == "12:00"
    assert config.skin_price_max_items_per_group == 20
    assert config.skin_price_localization_cache_ttl_seconds == 86400
    assert config.group_level_removal_grace_days == 15
    assert config.group_level_removal_max_per_day == 10
    assert config.content_relay_enabled is True
    assert config.content_relay_source_groups == ["456", "789"]
    assert config.content_relay_target_group == "123"
    assert config.content_relay_priority_source_groups == ["789"]
    assert config.content_relay_priority_cooldown_seconds == 900
    assert config.content_relay_secondary_daily_limit == 3
    assert config.ai_enabled is True
    assert config.ai_api_key == "sk-test-secret"
    assert config.ai_model == "deepseek-v4-flash-ascend"
    assert config.ai_vision_model == "qwen3.6-chat"
    assert config.ai_vision_max_tokens == 1200
    assert config.ai_timeout_seconds is None
    assert config.ai_max_tokens == 4096
    assert config.ai_max_history_messages == 6
    assert config.ai_max_input_chars == 16000
    assert config.ai_max_concurrent_requests == 20
    assert config.ai_group_shared_context_enabled is True
    assert config.ai_allow_concurrent_same_chat is True
    assert config.ai_multimodal_enabled is True
    assert config.ai_max_images == 2
    assert config.ai_image_max_bytes == 4000000
    assert config.ai_image_timeout_seconds == 12
    assert config.ai_member_memory_enabled is True
    assert config.ai_member_memory_refresh_interval_seconds == 21600
    assert config.ai_member_memory_max_members == 50
    assert config.ai_member_memory_max_context_chars == 12000
    assert config.ai_group_memory_enabled is True
    assert config.ai_group_memory_max_entries == 30
    assert config.ai_group_memory_max_entries_per_member == 6
    assert config.ai_group_memory_max_content_chars == 240
    assert config.ai_group_memory_max_context_chars == 7000
    assert config.ts_enabled is True
    assert config.ts_query_host == "teamspeak-query"
    assert config.ts_query_port == 10011
    assert config.ts_public_address == "ts.example.com"
    assert config.welcome_new_members is False


def test_plugin_config_accepts_group_game_schedule_keys() -> None:
    config = PWCS2Config.model_validate(
        {
            "communityInitialBalance": 200,
            "communityManagerQqIds": ["100000011"],
            "predictionEnabled": False,
            "predictionDurationMinutes": 45,
            "autoPredictionEnabled": False,
            "autoPredictionExpiryHours": 6,
            "autoPredictionPollIntervalSeconds": 45,
            "autoPredictionWatchConcurrency": 6,
            "autoPredictionMaxTotalScore": 4,
            "grenadeQuizQuestionTime": "18:30",
            "grenadeQuizRevealTime": "20:00",
            "weeklyReportTime": "22:15",
            "weeklyReportWeekday": 5,
        }
    )

    assert config.community_initial_balance == 200
    assert config.community_manager_qq_ids == ["100000011"]
    assert config.prediction_enabled is False
    assert config.prediction_duration_minutes == 45
    assert config.auto_prediction_enabled is False
    assert config.auto_prediction_expiry_hours == 6
    assert config.auto_prediction_poll_interval_seconds == 45
    assert config.auto_prediction_watch_concurrency == 6
    assert config.auto_prediction_max_total_score == 4
    assert config.grenade_quiz_question_time == "18:30"
    assert config.grenade_quiz_reveal_time == "20:00"
    assert config.weekly_report_time == "22:15"
    assert config.weekly_report_weekday == 5


def test_prediction_features_default_to_disabled() -> None:
    config = PWCS2Config()

    assert config.prediction_enabled is False
    assert config.auto_prediction_enabled is False


def test_group_ai_memory_defaults_and_limits() -> None:
    config = PWCS2Config()

    assert config.ai_group_memory_enabled is True
    assert config.ai_group_memory_max_entries == 20
    assert config.ai_group_memory_max_entries_per_member == 5
    assert config.ai_group_memory_max_content_chars == 200
    assert config.ai_group_memory_max_context_chars == 6000

    with pytest.raises(
        ValidationError,
        match="aiGroupMemoryMaxEntriesPerMember must not exceed",
    ):
        PWCS2Config.model_validate(
            {
                "aiGroupMemoryMaxEntries": 3,
                "aiGroupMemoryMaxEntriesPerMember": 4,
            }
        )


@pytest.mark.parametrize(
    "url",
    (
        "",
        "/cs2-training.html",
        "javascript:alert(1)",
        "ftp://game.example.test/cs2-training.html",
        "https://user:secret@game.example.test/cs2-training.html",
    ),
)
def test_training_game_url_must_be_safe_http_url(url: str) -> None:
    with pytest.raises(
        ValidationError,
        match=r"trainingGameUrl must be a public HTTP\(S\) URL",
    ):
        PWCS2Config.model_validate({"trainingGameUrl": url})


def test_steam_presence_stale_cache_must_cover_fresh_cache() -> None:
    with pytest.raises(
        ValidationError,
        match="steamPresenceStaleTtlSeconds must be greater than or equal to",
    ):
        PWCS2Config.model_validate(
            {
                "steamPresenceCacheTtlSeconds": 1000,
                "steamPresenceStaleTtlSeconds": 500,
            }
        )


def test_valorant_stale_cache_must_cover_fresh_cache() -> None:
    with pytest.raises(
        ValidationError,
        match="valorantStaleTtlSeconds must be greater than or equal to",
    ):
        PWCS2Config.model_validate(
            {
                "valorantCacheTtlSeconds": 1000,
                "valorantStaleTtlSeconds": 500,
            }
        )
