"""Synthetic media + FCPXML for fcpkit tests (ffmpeg-generated, deleted with the temp dir)."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fcpkit.config import ffmpeg  # noqa: E402

FFMPEG = ffmpeg()


def make_video(path: Path, seconds: int = 10, loud: bool = True) -> Path:
    """30 fps test pattern with a 440 Hz tone (or near silence)."""
    vol = '0.8' if loud else '0.001'
    subprocess.run([FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=s=320x180:r=30:d={seconds}',
                    '-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=48000:duration={seconds}',
                    '-af', f'volume={vol}', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    '-shortest', str(path)], check=True)
    return path


def make_still(path: Path) -> Path:
    subprocess.run([FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180',
                    '-frames:v', '1', str(path)], check=True)
    return path


def asset(aid: str, src: Path, seconds: int = 10, still: bool = False) -> str:
    if still:
        return (f'<asset id="{aid}" name="{src.stem}" uid="U{aid}" start="0s" duration="0s" hasVideo="1" '
                f'format="r1"><media-rep kind="original-media" src="{src.as_uri()}"/></asset>')
    return (f'<asset id="{aid}" name="{src.stem}" uid="U{aid}" start="0s" duration="{seconds}s" hasVideo="1" '
            f'format="r1" hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000">'
            f'<media-rep kind="original-media" src="{src.as_uri()}"/></asset>')


def fcpxml(resources: str, spine: str, duration: str = '20s') -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    <resources>
        <format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>
        <effect id="rx" name="Cross Dissolve" uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/>
        <effect id="rt" name="Basic Title" uid=".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"/>
        {resources}
    </resources>
    <library>
        <event name="fcpkit test">
            <project name="t">
                <sequence format="r1" duration="{duration}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
                    <spine>
                        {spine}
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
'''


def write(dir_: Path, name: str, text: str) -> Path:
    p = Path(dir_) / name
    p.write_text(text, encoding='utf-8')
    return p
