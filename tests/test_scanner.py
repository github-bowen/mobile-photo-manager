import datetime
import os
import struct
import tempfile
import unittest

from server.scanner import (
    SCAN_VERSION,
    extract_motion_photo_metadata,
    extract_video_metadata,
    get_all_files_on_disk,
    scan_specific_files,
)


def fake_mp4() -> bytes:
    return struct.pack(">I4s4sI4s", 20, b"ftyp", b"isom", 0, b"mp42")


def modern_xmp(video_length: int, enabled: int = 1) -> bytes:
    return f"""
<x:xmpmeta xmlns:x="adobe:ns:meta/"
 xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:Camera="http://ns.google.com/photos/1.0/camera/"
 xmlns:Container="http://ns.google.com/photos/1.0/container/"
 xmlns:Item="http://ns.google.com/photos/1.0/container/item/">
 <rdf:RDF>
  <rdf:Description Camera:MotionPhoto="{enabled}" Camera:MotionPhotoVersion="1">
   <Container:Directory><rdf:Seq>
    <rdf:li Item:Mime="image/jpeg" Item:Semantic="Primary" Item:Length="0"/>
    <rdf:li Item:Mime="video/mp4" Item:Semantic="MotionPhoto" Item:Length="{video_length}"/>
   </rdf:Seq></Container:Directory>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
""".encode()


class MotionPhotoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def write(self, name: str, content: bytes) -> str:
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "wb") as file:
            file.write(content)
        return path

    def test_detects_standard_container_length(self):
        video = fake_mp4()
        image = b"\xff\xd8" + modern_xmp(len(video)) + b"\xff\xd9"
        path = self.write("PXL_20260801_120000_MP.jpg", image + video)

        metadata = extract_motion_photo_metadata(path)

        self.assertEqual(metadata["is_motion_photo"], 1)
        self.assertEqual(metadata["motion_photo_offset"], len(image))
        self.assertEqual(metadata["motion_photo_length"], len(video))
        self.assertEqual(metadata["motion_photo_mime"], "video/mp4")

    def test_detects_legacy_microvideo_offset(self):
        video = fake_mp4()
        xmp = f"""
<x:xmpmeta xmlns:x="adobe:ns:meta/"
 xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:GCamera="http://ns.google.com/photos/1.0/camera/">
 <rdf:RDF><rdf:Description GCamera:MicroVideo="1"
  GCamera:MicroVideoOffset="{len(video)}"/></rdf:RDF>
</x:xmpmeta>
""".encode()
        image = b"\xff\xd8" + xmp + b"\xff\xd9"
        path = self.write("legacy.jpg", image + video)

        metadata = extract_motion_photo_metadata(path)

        self.assertEqual(metadata["motion_photo_offset"], len(image))
        self.assertEqual(metadata["motion_photo_length"], len(video))

    def test_detects_samsung_marker(self):
        video = fake_mp4()
        image = b"\xff\xd8still-image\xff\xd9MotionPhoto_Data"
        path = self.write("samsung.jpg", image + video)

        metadata = extract_motion_photo_metadata(path)

        self.assertEqual(metadata["motion_photo_offset"], len(image))
        self.assertEqual(metadata["motion_photo_length"], len(video))

    def test_rejects_disabled_or_missing_video(self):
        path = self.write("disabled.jpg", b"\xff\xd8" + modern_xmp(20, enabled=0) + b"\xff\xd9")
        self.assertEqual(extract_motion_photo_metadata(path), {})

    def test_finds_deep_marker_straddling_backward_chunk_boundary(self):
        # A 3MB motion photo whose real marker straddles the 1MB backward
        # chunk boundary: the scan walks chunk 1 (last 1MB, empty), then
        # finds the marker only through chunk 2's head-extension overlap.
        # The decoy marker near the file start must never win: the scan
        # always reports the LAST marker in the window.
        marker = b"MotionPhoto_Data"
        video = fake_mp4()
        head = b"\xff\xd8decoy\xff\xd9" + marker  # decoy marker inside the 512KB head
        boundary_start = 1024 * 1024 - 8  # marker start; spans the 1MB line
        pad1 = b"\x00" * (boundary_start - len(head))
        tail_pad = b"\x00" * (
            3 * 1024 * 1024 - boundary_start - len(marker) - len(video)
        )
        content = head + pad1 + marker + video + tail_pad
        self.assertEqual(len(content), 3 * 1024 * 1024)
        path = self.write("deep_marker.jpg", content)

        metadata = extract_motion_photo_metadata(path)

        self.assertEqual(metadata["is_motion_photo"], 1)
        self.assertEqual(metadata["motion_photo_offset"], boundary_start + len(marker))
        self.assertEqual(
            metadata["motion_photo_length"], 3 * 1024 * 1024 - boundary_start - len(marker)
        )


_MVHD_CREATION = int(
    (datetime.datetime(2024, 5, 1, 12, 0, 0) - datetime.datetime(1904, 1, 1)).total_seconds()
)


def build_mvhd_v0(creation: int, timescale: int, duration: int) -> bytes:
    payload = b"\x00\x00\x00\x00"  # version 0 + flags
    payload += struct.pack(">IIII", creation, 0, timescale, duration)
    return struct.pack(">I4s", 8 + len(payload), b"mvhd") + payload


def build_tkhd_v0(width: int, height: int) -> bytes:
    # The parser reads width/height at fixed offsets from the "tkhd" token
    # (token+76/+80), so pad the atom body until those offsets land on
    # our 16.16 fixed-point values.
    body = b"\x00\x00\x00\x00" + b"\x00" * 68
    body += struct.pack(">II", width << 16, height << 16)
    return struct.pack(">I4s", 8 + len(body), b"tkhd") + body


def build_moov(creation: int, timescale: int, duration: int,
               width: int, height: int) -> bytes:
    body = build_mvhd_v0(creation, timescale, duration) + build_tkhd_v0(width, height)
    return struct.pack(">I4s", 8 + len(body), b"moov") + body


class ChunkedScanBoundaryTests(unittest.TestCase):
    """extract_video_metadata must never load whole multi-MB files into memory.

    GPS and moov discovery walk bounded chunks; these tests place tokens
    exactly across chunk boundaries, where a missing overlap would lose
    them. The fixtures are synthetic: only the parser's actual byte
    offsets matter, not spec-accurate atom layouts.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def write(self, name: str, content: bytes) -> str:
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "wb") as file:
            file.write(content)
        return path

    def test_gps_straddling_head_chunk_boundary(self):
        # 16-byte GPS string split across the 256KB chunk boundary at
        # 256KB-7..256KB+9 of a 1MB file. A parseable moov at the start
        # keeps the cv2 fallback out of the timing.
        gps = b"+47.3769+008.5416"
        moov = build_moov(_MVHD_CREATION, timescale=1000, duration=3500,
                          width=1920, height=1080)
        data = bytearray(1024 * 1024)
        data[0:len(moov)] = moov
        start = 256 * 1024 - 7
        data[start:start + len(gps)] = gps
        path = self.write("boundary_gps.mov", bytes(data))

        meta = extract_video_metadata(path)

        self.assertEqual(meta["latitude"], 47.3769)
        self.assertEqual(meta["longitude"], 8.5416)

    def test_gps_at_head_tail_junction_of_large_file(self):
        # A >5MB file whose only GPS string straddles the 5MB head limit:
        # the head pass cannot see it whole, so the tail pass must cover
        # the junction (old code searched head+tail concatenated). moov
        # in the head keeps the cv2 fallback out of the timing.
        gps = b"+47.3769+008.5416"
        file_size = 5 * 1024 * 1024 + 512 * 1024
        moov = build_moov(_MVHD_CREATION, timescale=1000, duration=3500,
                          width=1920, height=1080)
        data = bytearray(file_size)
        data[0:len(moov)] = moov
        start = 5 * 1024 * 1024 - 7  # spans [5MB-7, 5MB+9)
        data[start:start + len(gps)] = gps
        path = self.write("junction_gps.mov", bytes(data))

        meta = extract_video_metadata(path)

        self.assertEqual(meta["latitude"], 47.3769)
        self.assertEqual(meta["longitude"], 8.5416)

    def test_moov_in_tail_of_large_file(self):
        # moov+mvhd+tkhd beyond the 5MB head limit: duration, dimensions
        # and creation time must still parse from the tail walk.
        file_size = 5 * 1024 * 1024 + 512 * 1024
        moov = build_moov(_MVHD_CREATION, timescale=1000, duration=3500, width=1920, height=1080)
        moov_abs = 5 * 1024 * 1024 + 512  # inside the last 1MB, past the head
        data = bytearray(file_size)
        data[moov_abs:moov_abs + len(moov)] = moov
        path = self.write("tail_moov.mov", bytes(data))

        meta = extract_video_metadata(path)

        self.assertEqual(meta["taken_at"], "2024-05-01T12:00:00")
        self.assertEqual(meta["duration"], 3.5)
        self.assertEqual(meta["width"], 1920)
        self.assertEqual(meta["height"], 1080)


class AndroidImportTests(unittest.TestCase):
    def test_discovers_android_extensions_and_screenshot_names(self):
        with tempfile.TemporaryDirectory() as photos_dir:
            album = os.path.join(photos_dir, "202608")
            os.makedirs(album)
            filenames = ["photo.webp", "clip.mp4", "camera.heif", "modern.avif", "old.3gp"]
            for filename in filenames:
                with open(os.path.join(album, filename), "wb") as file:
                    file.write(b"not-media")
            with open(os.path.join(album, "ignored.txt"), "wb") as file:
                file.write(b"ignored")

            paths = get_all_files_on_disk(photos_dir)

            self.assertEqual(paths, {os.path.join("202608", name) for name in filenames})

            screenshot = "Screenshot_20260801_120000.webp"
            with open(os.path.join(album, screenshot), "wb") as file:
                file.write(b"not-media")
            records = scan_specific_files(
                photos_dir, {os.path.join("202608", screenshot)}
            )
            self.assertEqual(records[0]["is_screenshot"], 1)
            self.assertEqual(records[0]["scan_version"], SCAN_VERSION)


if __name__ == "__main__":
    unittest.main()
