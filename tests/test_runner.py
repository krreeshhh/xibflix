import asyncio
import os
import shutil
import subprocess
import unittest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop, TestClient, TestServer
from bot.helper.media_tracks import probe_media_tracks, clear_media_tracks_cache
from bot.server.stream_routes import routes, SUBTITLE_CACHE

FIXTURE_PATH = os.path.abspath("tests/fixtures/sample_multitrack.mkv")


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


class MediaTracksEndpointTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        ensure_fixture()

    async def asyncSetUp(self):
        self.app = web.Application()
        self.app.add_routes(routes)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_01_probe_media_tracks(self):
        clear_media_tracks_cache()
        data = await probe_media_tracks(
            chat_id=12345,
            message_id=67890,
            secure_hash="test12",
            stream_url=FIXTURE_PATH
        )
        audio = data["audio_tracks"]
        subtitles = data["subtitle_tracks"]

        self.assertEqual(len(audio), 2)
        self.assertEqual(audio[0]["stream_index"], 1)
        self.assertEqual(audio[0]["language"], "jpn")
        self.assertEqual(audio[0]["title"], "Japanese Original")
        self.assertEqual(audio[0]["codec"], "aac")

        self.assertEqual(audio[1]["stream_index"], 2)
        self.assertEqual(audio[1]["language"], "eng")
        self.assertEqual(audio[1]["title"], "English Dub")
        self.assertEqual(audio[1]["codec"], "aac")

        self.assertEqual(len(subtitles), 2)
        self.assertEqual(subtitles[0]["stream_index"], 3)
        self.assertEqual(subtitles[0]["language"], "eng")
        self.assertEqual(subtitles[0]["title"], "English Subtitles")
        self.assertTrue(subtitles[0]["is_text"])

        self.assertEqual(subtitles[1]["stream_index"], 4)
        self.assertEqual(subtitles[1]["language"], "spa")
        self.assertEqual(subtitles[1]["title"], "Spanish Subtitles")
        self.assertTrue(subtitles[1]["is_text"])

    async def test_02_api_tracks_endpoint(self):
        resp = await self.client.get(
            f"/api/tracks/12345?id=67890&hash=test12&stream_url={FIXTURE_PATH}"
        )
        self.assertEqual(resp.status, 200)
        payload = await resp.json()
        self.assertIn("audio_tracks", payload)
        self.assertIn("subtitle_tracks", payload)
        self.assertEqual(len(payload["audio_tracks"]), 2)
        self.assertEqual(len(payload["subtitle_tracks"]), 2)

    async def test_03_api_subtitles_endpoint(self):
        resp = await self.client.get(
            f"/api/subtitles/12345?id=67890&hash=test12&stream=3&stream_url={FIXTURE_PATH}"
        )
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.headers.get("Content-Type", "").startswith("text/vtt"))
        text = await resp.text()
        self.assertTrue(text.startswith("WEBVTT"))
        self.assertIn("Hello, welcome to Surf-TG!", text)
        self.assertIn("00:00.500 --> 00:02.000", text)
        # Verify caching
        cache_key = "-10012345_67890_3"
        self.assertIn(cache_key, SUBTITLE_CACHE)
        self.assertEqual(SUBTITLE_CACHE[cache_key], text)

    async def test_04_api_audio_endpoint_and_range_requests(self):
        # 1. Full audio request
        resp = await self.client.get(
            f"/api/audio/12345?id=67890&hash=test12&stream=2&stream_url={FIXTURE_PATH}"
        )
        self.assertEqual(resp.status, 200)
        body = await resp.read()
        self.assertGreater(len(body), 1000)

        # 2. HTTP Range request (Partial Content 206)
        resp_range = await self.client.get(
            f"/api/audio/12345?id=67890&hash=test12&stream=2&stream_url={FIXTURE_PATH}",
            headers={"Range": "bytes=0-1024"}
        )
        self.assertEqual(resp_range.status, 206)
        content_range = resp_range.headers.get("Content-Range", "")
        self.assertTrue(content_range.startswith("bytes 0-1024/"))
        range_body = await resp_range.read()
        self.assertEqual(len(range_body), 1025)

    async def test_05_audio_cache_pruning(self):
        audio_dir = os.path.join("cache", "audio")
        os.makedirs(audio_dir, exist_ok=True)

        for dummy_id in range(201, 208):
            resp = await self.client.get(
                f"/api/audio/88888?id={dummy_id}&hash=test12&stream=1&stream_url={FIXTURE_PATH}"
            )
            self.assertEqual(resp.status, 200)

        all_audio_files = [
            f for f in os.listdir(audio_dir)
            if f.startswith("audio_") and f.endswith(".m4a")
        ]
        self.assertLessEqual(len(all_audio_files), 5)

    async def test_06_invalid_parameters(self):
        # Missing hash
        resp1 = await self.client.get("/api/tracks/12345?id=67890")
        self.assertEqual(resp1.status, 400)

        # Missing id
        resp2 = await self.client.get("/api/tracks/12345?hash=test12")
        self.assertEqual(resp2.status, 400)

        # Missing stream on subtitles
        resp3 = await self.client.get("/api/subtitles/12345?id=67890&hash=test12")
        self.assertEqual(resp3.status, 400)


if __name__ == "__main__":
    unittest.main()
