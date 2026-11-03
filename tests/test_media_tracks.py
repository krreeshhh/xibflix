import asyncio
import os
import shutil
import subprocess
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop, TestClient, TestServer
from bot.helper.media_tracks import probe_media_tracks, clear_media_tracks_cache
from bot.server.stream_routes import routes, SUBTITLE_CACHE

FIXTURE_PATH = os.path.abspath("tests/fixtures/sample_multitrack.mkv")

@pytest.fixture(scope="session", autouse=True)
def ensure_fixture():
    os.makedirs("tests/fixtures", exist_ok=True)
    sub_en = "tests/fixtures/sub_en.srt"
    sub_es = "tests/fixtures/sub_es.srt"
    with open(sub_en, "w") as f:
        f.write("1\n00:00:00,500 --> 00:00:02,000\nHello, welcome to Surf-TG!\n\n")
    with open(sub_es, "w") as f:
        f.write("1\n00:00:00,500 --> 00:00:02,000\n¡Hola, bienvenido a Surf-TG!\n\n")

    if not os.path.exists(FIXTURE_PATH):
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
            "-i", sub_en,
            "-i", sub_es,
            "-map", "0:v", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-map", "1:a", "-c:a:0", "aac", "-metadata:s:a:0", "language=jpn", "-metadata:s:a:0", "title=Japanese Original",
            "-map", "2:a", "-c:a:1", "aac", "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=English Dub",
            "-map", "3:s", "-c:s:0", "srt", "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English Subtitles",
            "-map", "4:s", "-c:s:1", "srt", "-metadata:s:s:1", "language=spa", "-metadata:s:s:1", "title=Spanish Subtitles",
            FIXTURE_PATH
        ], check=True, capture_output=True)


@pytest.mark.asyncio
async def test_probe_media_tracks():
    clear_media_tracks_cache()
    data = await probe_media_tracks(
        chat_id=12345,
        message_id=67890,
        secure_hash="test12",
        stream_url=FIXTURE_PATH
    )

    audio_tracks = data["audio_tracks"]
    subtitle_tracks = data["subtitle_tracks"]

    assert len(audio_tracks) == 2
    assert audio_tracks[0]["stream_index"] == 1
    assert audio_tracks[0]["language"] == "jpn"
    assert audio_tracks[0]["title"] == "Japanese Original"
    assert audio_tracks[0]["codec"] == "aac"

    assert audio_tracks[1]["stream_index"] == 2
    assert audio_tracks[1]["language"] == "eng"
    assert audio_tracks[1]["title"] == "English Dub"
    assert audio_tracks[1]["codec"] == "aac"

    assert len(subtitle_tracks) == 2
    assert subtitle_tracks[0]["stream_index"] == 3
    assert subtitle_tracks[0]["language"] == "eng"
    assert subtitle_tracks[0]["title"] == "English Subtitles"
    assert subtitle_tracks[0]["is_text"] is True

    assert subtitle_tracks[1]["stream_index"] == 4
    assert subtitle_tracks[1]["language"] == "spa"
    assert subtitle_tracks[1]["title"] == "Spanish Subtitles"
    assert subtitle_tracks[1]["is_text"] is True


@pytest.mark.asyncio
async def test_api_tracks_endpoint():
    app = web.Application()
    app.add_routes(routes)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # Request /api/tracks with custom stream_url
        resp = await client.get(
            f"/api/tracks/12345?id=67890&hash=test12&stream_url={FIXTURE_PATH}"
        )
        assert resp.status == 200
        payload = await resp.json()
        assert "audio_tracks" in payload
        assert "subtitle_tracks" in payload
        assert len(payload["audio_tracks"]) == 2
        assert len(payload["subtitle_tracks"]) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_subtitles_endpoint():
    app = web.Application()
    app.add_routes(routes)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # Request subtitle stream 3 (English)
        resp = await client.get(
            f"/api/subtitles/12345?id=67890&hash=test12&stream=3&stream_url={FIXTURE_PATH}"
        )
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "text/vtt"
        text = await resp.text()
        assert text.startswith("WEBVTT")
        assert "Hello, welcome to Surf-TG!" in text
        assert "00:00:00" in text

        # Verify caching
        cache_key = "-10012345_67890_3"
        assert cache_key in SUBTITLE_CACHE
        assert SUBTITLE_CACHE[cache_key] == text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_audio_endpoint_and_range():
    app = web.Application()
    app.add_routes(routes)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # Request audio stream 2 (English)
        resp = await client.get(
            f"/api/audio/12345?id=67890&hash=test12&stream=2&stream_url={FIXTURE_PATH}"
        )
        assert resp.status == 200
        body = await resp.read()
        assert len(body) > 1000

        # Request with Range header
        resp_range = await client.get(
            f"/api/audio/12345?id=67890&hash=test12&stream=2&stream_url={FIXTURE_PATH}",
            headers={"Range": "bytes=0-1024"}
        )
        assert resp_range.status == 206
        assert resp_range.headers.get("Content-Range", "").startswith("bytes 0-1024/")
        range_body = await resp_range.read()
        assert len(range_body) == 1025
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_audio_cache_pruning():
    audio_dir = os.path.join("cache", "audio")
    os.makedirs(audio_dir, exist_ok=True)

    # Clean prior test files in cache/audio
    for f in os.listdir(audio_dir):
        if f.startswith("audio_test_"):
            try:
                os.remove(os.path.join(audio_dir, f))
            except OSError:
                pass

    app = web.Application()
    app.add_routes(routes)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # Generate 6 audio requests
        for dummy_id in range(101, 107):
            resp = await client.get(
                f"/api/audio/99999?id={dummy_id}&hash=test12&stream=1&stream_url={FIXTURE_PATH}"
            )
            assert resp.status == 200

        all_audio_files = [
            f for f in os.listdir(audio_dir)
            if f.startswith("audio_") and f.endswith(".m4a")
        ]
        # Max cache threshold is 5
        assert len(all_audio_files) <= 5
    finally:
        await client.close()
