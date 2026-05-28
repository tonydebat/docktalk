# DockTalk

Voice assistant for Bike Share Toronto riders. Speak a return destination; DockTalk recommends a dock station and monitors availability while you ride.

## Requirements

- Python 3.13+
- A [Gemini API key](https://aistudio.google.com/app/apikey)

## Setup

**Clone and enter the repo**
```bash
git clone https://github.com/tonydebat/docktalk.git
cd docktalk
```

**Create and activate a virtual environment**
```bash
python3 -m venv venv
source venv/bin/activate

# Windows
venv\Scripts\Activate.ps1
```

**Install dependencies**
```bash
pip install -e ".[dev]"
```

**Add your API keys**
```bash
cp .env.example .env
# Edit .env and add your GEMINI_API_KEY
```

## Run

**Streamlit UI (recommended)**
```bash
streamlit run app/streamlit_app.py
```

**API server (local browser only)**
```bash
uvicorn app.server:app --reload
```

**API server (external access over HTTPS)**

First, install the local certificate (one-time setup):
```bash
./scripts/install_cert.sh
```

Then start the server:
```bash
uvicorn app.server:app --reload --host 0.0.0.0 --port 8000 --ssl-keyfile .certs/key.pem --ssl-certfile .certs/cert.pem
```