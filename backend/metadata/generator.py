import json
import os
from datetime import datetime
from typing import List, Optional


def _fmt_mmss(seconds: float) -> str:
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


class MetadataGenerator:
    def __init__(self):
        os.makedirs("logs", exist_ok=True)

    def generate(
        self,
        base_name: str,
        start_time: float,
        end_time: float,
        duration: int,
        participants: List[str],
        speaker_timeline: Optional[List[dict]] = None,
    ) -> str:
        """Generate and save a JSON metadata file.

        speaker_timeline (optional): list of dicts with keys:
            {speaker: str, start: float, end: float}
            where start/end are seconds elapsed since meeting start.
        """
        metadata = {
            "start_time":   datetime.fromtimestamp(start_time).isoformat(),
            "end_time":     datetime.fromtimestamp(end_time).isoformat(),
            "duration":     _fmt_mmss(duration),
            "participants": participants,
        }

        if speaker_timeline is not None:
            formatted = []
            for seg in speaker_timeline:
                formatted.append({
                    "speaker":    seg["speaker"],
                    "start":      seg["start"],
                    "end":        seg["end"],
                    "time_range": f"{_fmt_mmss(seg['start'])}-{_fmt_mmss(seg['end'])}",
                })
            metadata["speaker_timeline"] = formatted

        output_file = f"logs/{base_name}.json"
        with open(output_file, "w") as f:
            json.dump(metadata, f, indent=4)

        print(f"[{base_name}] Metadata saved → {output_file}")
        return os.path.abspath(output_file)
