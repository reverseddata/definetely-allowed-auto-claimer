import pytest

from conftest import D, make_settings
from mm2claimer.config import ConfigError, load_settings, parse_markets
from mm2claimer.models import norm_name, parse_price, search_term


def env(tmp_path, **kw):
    base = {"DATA_DIR": str(tmp_path), "LOG_DIR": str(tmp_path)}
    base.update(kw)
    return base


def test_defaults(tmp_path):
    s = make_settings(tmp_path)
    assert s.mode == "monitor" and s.markets == {"bhopp", "bloxymarket"}
    assert [(w.item, w.market, w.max_price) for w in s.watch] == [
        ("Harvester", "both", D("6")), ("Chroma Seer", "both", D("1"))]


def test_watch_syntax(tmp_path):
    s = load_settings(None, env=env(tmp_path, WATCH="Harvester@bhopp<=3.5 x2-4, "
                                                    "C. Seer@bloxymarket<=1, Seer"))
    h, c, seer = s.watch
    assert (h.market, h.max_price, h.min_qty, h.max_qty) == ("bhopp", D("3.5"), 2, 4)
    assert (c.key, c.market, c.watch_id) == ("chroma seer", "bloxymarket",
                                             "bloxymarket:chroma seer")
    assert seer.max_price is None and seer.watch_id == "seer"


def test_same_item_on_two_sites_is_allowed_twice_on_one_is_not(tmp_path):
    s = load_settings(None, env=env(tmp_path, WATCH="Seer@bhopp<=1, Seer@bloxymarket<=2"))
    assert len(s.watch) == 2
    with pytest.raises(ConfigError) as e:
        load_settings(None, env=env(tmp_path, WATCH="Seer<=1, seer<=2"))
    assert "twice" in str(e.value)


def test_bad_values_all_reported(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_settings(None, env=env(tmp_path, MODE="yolo", MARKETS="bloxswaps",
                                    WATCH="Seer@ebay<=1, Harvester<=0", POLL_INTERVAL_SECONDS="-1",
                                    MAX_CLAIM_QUANTITY="x"))
    text = " ".join(e.value.problems)
    for bit in ("MODE", "MARKETS", "market must be", "never matches", "POLL_INTERVAL",
                "MAX_CLAIM_QUANTITY"):
        assert bit in text


def test_poll_interval_zero_is_allowed(tmp_path):
    assert make_settings(tmp_path, extra_env={"POLL_INTERVAL_SECONDS": "0"}
                         ).poll_interval_seconds == 0


def test_claim_mode_needs_webhook_session_and_limits(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_settings(None, env=env(tmp_path, MODE="claim", WATCH="Seer"))
    text = " ".join(e.value.problems)
    assert "DISCORD_WEBHOOK_URL" in text and "BHOPP_TOKEN" in text and "any price" in text
    s = make_settings(tmp_path, "claim", watch="Seer", extra_env={"MAX_CLAIM_PRICE": "2"})
    assert s.max_claim_price == D("2")


def test_one_session_is_enough(tmp_path):
    s = load_settings(None, env=env(tmp_path, MODE="claim", WATCH="Seer<=1",
                                    DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/1/x",
                                    BLOXYMARKET_COOKIE="Cookie: a=b"))
    assert s.bloxymarket_cookie == "a=b" and s.bhopp_token == ""


def test_bearer_prefix_is_stripped(tmp_path):
    s = make_settings(tmp_path, extra_env={"BHOPP_TOKEN": "Bearer abc.def.ghi"})
    assert s.bhopp_token == "abc.def.ghi"


def test_deposit_side_is_refused_in_toml(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[watch]]\nitem = "Seer"\nside = "deposit"\n')
    with pytest.raises(ConfigError) as e:
        load_settings(cfg, env=env(tmp_path))
    assert "no deposit side" in str(e.value)


def test_secrets_never_in_redacted(tmp_path):
    s = make_settings(tmp_path, "claim")
    text = str(s.redacted()) + repr(s)
    assert "test-token-not-real" not in text and "test-cookie-not-real" not in text


def test_parse_markets():
    assert parse_markets("") == parse_markets("both") == {"bhopp", "bloxymarket"}
    assert parse_markets("bhopp") == {"bhopp"}
    assert parse_markets("starpets") is None


def test_names_and_search_terms():
    assert norm_name("C. Seer") == "chroma seer"
    assert norm_name("Traveler's Axe") == "traveler axe"
    assert search_term("chroma seer") == "seer"
    assert search_term("traveler axe") == "traveler"
    assert search_term("chroma") == "chroma"


def test_parse_price():
    assert parse_price("1,200.5") == D("1200.5") and parse_price(0.01) == D("0.01")
    assert parse_price(-1) is None and parse_price(float("nan")) is None
    assert parse_price(True) is None and parse_price("x") is None
