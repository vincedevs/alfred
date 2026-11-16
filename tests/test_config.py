"""Settings: only the OpenAI key is required, and secrets never show."""

import pytest

from alfred.config import PROJECT_ROOT, SECRET_SETTINGS, ConfigError, load_config

KEY = {"OPENAI_API_KEY": "sk-test"}


def test_defaults_need_only_the_api_key():
    config = load_config(KEY)
    assert (config.live_model, config.delegate_model) == ("gpt-live-1", "gpt-6-luna")
    assert config.agent_model == "claude-haiku-5-5"
    assert config.wakeword_dir == PROJECT_ROOT / "models"
    assert config.github_token is None and config.web_port is None
    assert config.projects_dir == config.data_dir / "projects"
    assert config.skills_dir == config.data_dir / "skills"


def test_missing_api_key_explains_the_fix():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_config({})


def test_values_are_parsed():
    config = load_config(
        KEY
        | {
            "ALFRED_WAKE_THRESHOLD": "0.7",
            "ALFRED_INPUT_DEVICE": "2",
            "ALFRED_OUTPUT_DEVICE": "USB Speaker",
            "ALFRED_LOG_LEVEL": "debug",
            "ALFRED_AGENT_MODEL": "claude-sonnet-5-5",
            "ALFRED_AGENT_MAX_TURNS": "50",
            "ALFRED_ECHO_CANCELLATION": "off",
        }
    )
    assert config.wake_threshold == 0.7
    assert config.input_device == 2 and config.output_device == "USB Speaker"
    assert config.log_level == "DEBUG" and config.echo_cancellation is False
    assert config.agent_model == "claude-sonnet-5-5" and config.agent_max_turns == 50


@pytest.mark.parametrize(
    "setting",
    [
        {"ALFRED_WAKE_THRESHOLD": "1.5"},
        {"ALFRED_ECHO_CANCELLATION": "maybe"},
        {"ALFRED_TIMEZONE": "Mars/Base"},
        {"ALFRED_WEB_PORT": "80"},
    ],
)
def test_bad_values_are_rejected(setting):
    with pytest.raises(ConfigError, match=next(iter(setting))):
        load_config(KEY | setting)


def test_the_github_token_stays_secret():
    config = load_config(KEY | {"ALFRED_GITHUB_TOKEN": " github_pat_abc "})
    assert config.github_token == "github_pat_abc"
    assert "github_pat_abc" not in repr(config) and "github_token" in SECRET_SETTINGS


def test_every_prompt_exists_and_they_stay_short():
    words = 0
    for name in ("voice", "delegate", "agent", "memory"):
        text = (PROJECT_ROOT / "prompts" / f"{name}.md").read_text()
        words += len(text.split())
    assert words < 900  # the plan's budget
