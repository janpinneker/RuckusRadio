"""Spotify F2: Wiedergabe-Helfer und SpotifyPlayerService gegen Attrappen (Spec §13)."""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-spotify-player-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
os.environ.pop("RUCKUS_SPOTIFY_CLIENT_ID", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from soundboard import protocol as p, spotify, spotify_player as sp  # noqa: E402
import core_fakes  # noqa: E402

TRACK = {"id": "t1", "uri": "spotify:track:t1", "name": "Neon Drift", "duration_ms": 214000,
         "type": "track", "artists": [{"name": "Kassette 84"}],
         "album": {"name": "Nachtfahrt", "images": [{"url": "https://i.scdn.co/a"}]}}
DEVICE = {"id": "d1", "name": "DESKTOP-JAN", "type": "Computer", "volume_percent": 60,
          "supports_volume": True, "is_active": True}
PLAYER = {"is_playing": True, "progress_ms": 81234, "item": TRACK, "device": DEVICE,
          "shuffle_state": True, "repeat_state": "context",
          "context": {"uri": "spotify:playlist:p1"}}


def test_map_player_and_device():
    state = sp.map_player(PLAYER, now=100.0)
    assert state["is_playing"] is True and state["progress_ms"] == 81234
    assert state["fetched_at"] == 100.0
    assert state["track"]["title"] == "Neon Drift" and state["track"]["cover_url"] == "https://i.scdn.co/a"
    assert state["context_uri"] == "spotify:playlist:p1"
    assert state["shuffle"] is True and state["repeat"] == "context"
    assert state["device"] == {"id": "d1", "name": "DESKTOP-JAN", "type": "Computer",
                               "volume_percent": 60, "supports_volume": True, "active": True}
    nothing = sp.map_player({}, now=5.0)
    assert nothing["track"] is None and nothing["is_playing"] is False and nothing["device"] is None
    episode = sp.map_player({**PLAYER, "item": {"type": "episode", "name": "Pod"}}, now=1.0)
    assert episode["track"] is None, "episodes and ads show as nothing (F2)"
    odd = sp.map_player({**PLAYER, "repeat_state": "weird", "device": None}, now=1.0)
    assert odd["repeat"] == "off" and odd["device"] is None
    assert sp.map_device({"id": "x"})["volume_percent"] is None
    print("player and device map from Spotify's shape, 204 means nothing plays: OK")


def test_pick_device():
    phone = {"id": "p", "name": "Handy", "type": "Smartphone", "active": False}
    pc = {"id": "c", "name": "desktop-jan", "type": "Computer", "active": False}
    other = {"id": "o", "name": "Laptop", "type": "Computer", "active": False}
    active = {**phone, "active": True}
    assert sp.pick_device([phone, pc, other], "DESKTOP-JAN", "o")["id"] == "c"
    assert sp.pick_device([phone, other], "DESKTOP-JAN", "o")["id"] == "o"
    assert sp.pick_device([active, pc], "DESKTOP-JAN", "") ["id"] == "p"
    assert sp.pick_device([phone], "DESKTOP-JAN", "gone") is None
    assert sp.pick_device([], "DESKTOP-JAN", "o") is None
    print("pick: active > this PC > remembered > none: OK")


def test_play_body_and_poll_delay():
    assert sp.play_body(("spotify:track:t1",), "", "") == {"uris": ["spotify:track:t1"]}
    assert sp.play_body((), "spotify:playlist:p1", "") == {"context_uri": "spotify:playlist:p1"}
    assert sp.play_body((), "spotify:playlist:p1", "spotify:track:t1") == {
        "context_uri": "spotify:playlist:p1", "offset": {"uri": "spotify:track:t1"}}
    assert sp.play_body((), "", "") == {}
    assert sp.next_poll_delay(True) == sp.POLL_PLAYING == 5.0
    assert sp.next_poll_delay(False) == sp.POLL_IDLE == 20.0
    assert sp.next_poll_delay(True, retry_after=30.0) == 30.0
    print("play body and poll rhythm: OK")


def _core(api, *, connected=True, hostname="DESKTOP-JAN", viewers=1):
    c, events = core_fakes.make_core(spotify_api=api)
    c.store.data["spotify_client_id"] = "cid"
    c.spotify.tokens.clear()
    c.spotify_player._clock = lambda: 1000.0 + c.executor.now  # follows advance()
    c.spotify_player._hostname = hostname
    c.spotify_player.attach_viewers(lambda: viewers)
    c.start()
    if connected:
        c.spotify.finish_login("r1", {"display_name": "Jan"})
    return c, events


def test_part_is_null_without_connection_and_commands_refuse():
    api = core_fakes.FakeSpotifyApi(player=PLAYER)
    c, events = _core(api, connected=False)
    assert c.state()["spotify_player"] is None
    c.send(p.SpotifyPause())
    assert api.sent == [], "no call without a connection"
    assert any("Nicht mit Spotify verbunden" in e.message for e in core_fakes.of_type(events, p.SpotifyError))
    print("without a connection the part is null and commands refuse: OK")


def test_pause_is_optimistic_then_refreshed():
    api = core_fakes.FakeSpotifyApi(player=PLAYER)
    c, _events = _core(api)
    c.spotify_player.refresh_now()
    assert c.state()["spotify_player"]["is_playing"] is True
    api.player = {**PLAYER, "is_playing": False}
    c.send(p.SpotifyPause())
    assert api.sent[-1][:2] == ("PUT", "/me/player/pause")
    assert c.state()["spotify_player"]["is_playing"] is False, "optimistic"
    gets = len([x for x in api.calls if x[1] == "/me/player"])
    c.executor.advance(sp.REFRESH_AFTER_COMMAND)
    assert len([x for x in api.calls if x[1] == "/me/player"]) == gets + 1, "refresh after the command"
    state = c.state()["spotify_player"]
    assert "token" not in json.dumps(state).lower() and "cid" not in json.dumps(state)
    print("pause flips at once and is confirmed ~0.7 s later: OK")


def test_failed_command_reports_and_takes_the_truth_back():
    api = core_fakes.FakeSpotifyApi(player=PLAYER, send_fail=spotify.PremiumRequired())
    c, events = _core(api)
    c.spotify_player.refresh_now()
    c.send(p.SpotifyPause())
    state = c.state()["spotify_player"]
    assert state["is_playing"] is True, "the refresh after a failure undoes the optimism"
    assert any(e.message == "Steuern braucht Spotify Premium."
               for e in core_fakes.of_type(events, p.SpotifyError)), "the notice still fired once"
    assert state["error"] == "Steuern braucht Spotify Premium.", \
        "a command error stays readable until the next successful command, not wiped by the corrective refresh"
    print("a refused command says why, and stays readable through the refresh that follows: OK")


def test_play_without_device_picks_this_pc_and_remembers_it():
    pc = {"id": "c", "name": "DESKTOP-JAN", "type": "Computer", "is_active": False,
          "volume_percent": 50, "supports_volume": True}
    api = core_fakes.FakeSpotifyApi(devices=[pc],
                                    send_fail={("PUT", "/me/player/play"): spotify.NoDevice()})
    c, _events = _core(api)
    c.send(p.SpotifyPlay(uris=("spotify:track:t1",)))
    plays = [s for s in api.sent if s[1] == "/me/player/play"]
    assert len(plays) == 2 and plays[1][2] == {"device_id": "c"}, plays
    assert plays[1][3] == {"uris": ["spotify:track:t1"]}
    assert c.store.data["spotify_device_id"] == "c"
    print("play without an active device goes to this PC and remembers it: OK")


def test_play_without_any_device_says_open_spotify():
    api = core_fakes.FakeSpotifyApi(devices=[], send_fail={("PUT", "/me/player/play"): spotify.NoDevice()})
    c, events = _core(api)
    c.send(p.SpotifyResume())
    assert any(e.message == "Öffne die Spotify-App auf diesem Rechner."
               for e in core_fakes.of_type(events, p.SpotifyError)), "the notice still names the reason"
    assert c.state()["spotify_player"]["error"] == "Öffne die Spotify-App auf diesem Rechner.", \
        "a command error stays readable until the next successful command"
    print("no device at all: open the Spotify app: OK")


def test_transfer_devices_and_setters():
    api = core_fakes.FakeSpotifyApi(player=PLAYER, devices=[DEVICE])
    c, _events = _core(api)
    c.spotify_player.refresh_now()
    c.send(p.SpotifyLoadDevices())
    assert c.state()["spotify_player"]["devices"][0]["id"] == "d1"
    c.send(p.SpotifyTransfer("d1"))
    assert api.sent[-1] == ("PUT", "/me/player", {}, {"device_ids": ["d1"], "play": True})
    assert c.store.data["spotify_device_id"] == "d1"
    c.send(p.SpotifySetVolume(140))
    assert api.sent[-1][2] == {"volume_percent": 100}, "clamped"
    assert c.state()["spotify_player"]["device"]["volume_percent"] == 100
    c.send(p.SpotifySetShuffle(False))
    assert api.sent[-1][:3] == ("PUT", "/me/player/shuffle", {"state": False})
    c.send(p.SpotifySetRepeat("track"))
    assert api.sent[-1][2] == {"state": "track"} and c.state()["spotify_player"]["repeat"] == "track"
    before = len(api.sent)
    c.send(p.SpotifySetRepeat("nonsense"))
    assert len(api.sent) == before, "an unknown repeat mode is dropped"
    c.send(p.SpotifySeek(5000))
    assert api.sent[-1][:3] == ("PUT", "/me/player/seek", {"position_ms": 5000})
    assert c.state()["spotify_player"]["progress_ms"] == 5000
    c.send(p.SpotifyNext())
    c.send(p.SpotifyPrevious())
    c.send(p.SpotifyAddToQueue("spotify:track:t2"))
    assert [s[:2] for s in api.sent[-3:]] == [("POST", "/me/player/next"),
                                             ("POST", "/me/player/previous"),
                                             ("POST", "/me/player/queue")]
    assert api.sent[-1][2] == {"uri": "spotify:track:t2"}
    print("devices, transfer, volume, shuffle, repeat, seek, next, prev, queue: OK")


def test_seek_while_a_seek_runs_sends_only_the_latest_after_it():
    api = core_fakes.FakeSpotifyApi(player=PLAYER)
    c, _events = _core(api)
    fired = []

    def reenter(method, path, params):
        if path == "/me/player/seek" and not fired:
            fired.append(1)
            c.send(p.SpotifySeek(2000))   # arrives while the first seek is in flight
            c.send(p.SpotifySeek(3000))
    api.on_send = reenter
    c.send(p.SpotifySeek(1000))
    seeks = [s[2]["position_ms"] for s in api.sent if s[1] == "/me/player/seek"]
    assert seeks == [1000, 3000], seeks
    print("seek/volume coalesce: only the newest value follows a running call: OK")


def _gets(api):
    return len([x for x in api.calls if x[0] == "get" and x[1] == "/me/player"])


def test_polling_rhythm_follows_playback_and_viewers():
    viewers = [1]
    api = core_fakes.FakeSpotifyApi(player=PLAYER)
    c, _events = _core(api)
    c.spotify_player.attach_viewers(lambda: viewers[0])
    c.executor.advance(0.0)                 # start tick
    first = _gets(api)
    assert first >= 1, "polls at once when someone watches"
    c.executor.advance(sp.POLL_PLAYING)
    assert _gets(api) == first + 1, "5 s while music plays"
    api.player = {**PLAYER, "is_playing": False}
    c.executor.advance(sp.POLL_PLAYING)
    n = _gets(api)
    c.executor.advance(sp.POLL_PLAYING)
    assert _gets(api) == n, "paused: not every 5 s"
    c.executor.advance(sp.POLL_IDLE)
    assert _gets(api) == n + 1, "paused: every 20 s"
    viewers[0] = 0
    c.executor.advance(sp.POLL_IDLE * 3)
    assert _gets(api) == n + 1, "no viewer, no network"
    viewers[0] = 1
    c.executor.advance(sp.POLL_IDLE)
    assert _gets(api) == n + 2, "the quiet alarm notices a new viewer"
    print("poll 5 s playing / 20 s idle, silent without viewers: OK")


def test_rate_limit_quiets_polling_and_logout_stops_it():
    api = core_fakes.FakeSpotifyApi(player=PLAYER)
    c, _events = _core(api)
    c.executor.advance(0.0)
    api.fail = spotify.RateLimitError(60.0)
    c.executor.advance(sp.POLL_PLAYING)       # this poll hits 429
    api.fail = None
    n = _gets(api)
    c.executor.advance(50.0)
    assert _gets(api) == n, "quiet until Retry-After"
    c.executor.advance(sp.POLL_IDLE)
    assert _gets(api) > n
    c.send(p.SpotifyLogout())
    assert c.state()["spotify_player"] is None
    n = _gets(api)
    c.executor.advance(sp.POLL_IDLE * 3)
    assert _gets(api) == n, "logout stops the network"
    print("429 quiets polling, logout stops it: OK")


def test_polling_resumes_after_a_relogin_following_a_401():
    api = core_fakes.FakeSpotifyApi(player=PLAYER)
    c, _events = _core(api)
    c.executor.advance(0.0)                    # initial poll succeeds
    api.fail = spotify.AuthError()
    c.executor.advance(sp.POLL_PLAYING)         # this poll hits a 401, login drops
    assert c.state()["spotify_player"] is None
    api.fail = None
    c.spotify.finish_login("r2", {"display_name": "Jan"})
    n = _gets(api)
    c.executor.advance(sp.POLL_IDLE)
    assert _gets(api) > n, "polling resumes once logged back in"
    print("a 401 during polling still leaves a timer running, so a relogin resumes it: OK")


def test_rate_limit_on_a_command_waits_for_retry_after():
    api = core_fakes.FakeSpotifyApi(player=PLAYER, send_fail=spotify.RateLimitError(30.0))
    c, _events = _core(api)
    n = _gets(api)
    c.send(p.SpotifyPause())
    assert _gets(api) == n, "Retry-After holds off the immediate refresh"
    c.executor.advance(30.0)
    assert _gets(api) > n, "the GET happens once Retry-After elapses"
    print("a 429 on a command waits for Retry-After instead of refreshing at once: OK")


def test_error_clears_once_a_poll_succeeds_again():
    api = core_fakes.FakeSpotifyApi(player=PLAYER)
    c, _events = _core(api)
    c.executor.advance(0.0)
    api.fail = spotify.NetworkError()
    c.executor.advance(sp.POLL_PLAYING)
    assert c.state()["spotify_player"]["error"] is not None
    api.fail = None
    c.executor.advance(sp.POLL_IDLE)
    assert c.state()["spotify_player"]["error"] is None
    print("the player error clears once a poll succeeds again: OK")


def test_command_error_clears_once_a_command_succeeds_again():
    api = core_fakes.FakeSpotifyApi(player=PLAYER, send_fail=spotify.PremiumRequired())
    c, _events = _core(api)
    c.spotify_player.refresh_now()
    c.send(p.SpotifyPause())
    assert c.state()["spotify_player"]["error"] == "Steuern braucht Spotify Premium."
    api.send_fail = None
    c.send(p.SpotifyPause())
    assert c.state()["spotify_player"]["error"] is None, "a later successful command clears it"
    print("a command error clears once a command succeeds again: OK")


def test_unexpected_exceptions_dont_leak_raw_text_to_the_ui():
    api = core_fakes.FakeSpotifyApi(player=PLAYER, fail=RuntimeError("boom"),
                                    send_fail=RuntimeError("boom"))
    c, _events = _core(api)
    c.send(p.SpotifyPause())
    state = c.state()["spotify_player"]
    assert state["error"] == spotify.NetworkError.text
    assert "boom" not in state["error"]
    print("a raw exception never reaches the UI text, only the log: OK")


def main():
    test_map_player_and_device()
    test_pick_device()
    test_play_body_and_poll_delay()
    test_part_is_null_without_connection_and_commands_refuse()
    test_pause_is_optimistic_then_refreshed()
    test_failed_command_reports_and_takes_the_truth_back()
    test_play_without_device_picks_this_pc_and_remembers_it()
    test_play_without_any_device_says_open_spotify()
    test_transfer_devices_and_setters()
    test_seek_while_a_seek_runs_sends_only_the_latest_after_it()
    test_polling_rhythm_follows_playback_and_viewers()
    test_rate_limit_quiets_polling_and_logout_stops_it()
    test_polling_resumes_after_a_relogin_following_a_401()
    test_rate_limit_on_a_command_waits_for_retry_after()
    test_error_clears_once_a_poll_succeeds_again()
    test_command_error_clears_once_a_command_succeeds_again()
    test_unexpected_exceptions_dont_leak_raw_text_to_the_ui()
    print("\nALL SPOTIFY PLAYER CHECKS PASSED")


if __name__ == "__main__":
    main()
