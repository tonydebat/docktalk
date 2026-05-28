```
source venv/bin/activate
```

## Add keys
```
cp .env.example .env
# Add your keys to .env
```

## Run locally only in browser
```
uvicorn app.server:app --reload 
```

## Install local certificate
```
./scripts/install_cert.sh
```

## Run so that external clients can access the server
```
uvicorn app.server:app --reload --host 0.0.0.0 --port 8000 --ssl-keyfile .certs/key.pem --ssl-certfile .certs/cert.pem
```