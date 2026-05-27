import json


def build_speech_synthesis_html(message: str) -> str:
    text = message.strip()
    if not text:
        raise ValueError("message must not be empty")

    safe_text = json.dumps(text).replace("</", "<\\/")

    return f"""
<script>
const message = {safe_text};
const utterance = new SpeechSynthesisUtterance(message);
utterance.rate = 1;
utterance.pitch = 1;
window.speechSynthesis.cancel();
window.speechSynthesis.speak(utterance);
</script>
"""
