import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot


def test_progress_message_shows_phase_progress_time_and_escaped_values():
    progress = bot.Progress(None, 1, 1, "<student>")
    progress.phase = "بيحل الدرس"
    progress.current_activity = "درس <مهم>"
    progress.platform_progress = (2, 4)
    progress.initial_platform_done = 0
    progress.t0 = time.monotonic() - 40
    progress.clock_started = True          # الحل بدأ فعلًا (العدّاد شغّال)

    rendered = progress.render()

    assert "جاري الحل" in rendered
    assert "<b>بيحل الدرس</b>" in rendered
    assert "٥٠٪" in rendered
    assert "الوقت المنقضي" in rendered
    assert "&lt;student&gt;" in rendered
    assert "درس &lt;مهم&gt;" in rendered
    assert "الفصول المكتملة" in rendered


def test_invite_link_is_a_telegram_deep_link_with_referrer_payload():
    assert bot.invite_link("@sample_bot", 123456) == (
        "https://t.me/sample_bot?start=ref_123456"
    )
