# Anonymous Photo Chat

A simple real-time chat app with anonymous users, text chat, rooms, typing status, and photo sharing.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Open:

http://127.0.0.1:8000

## Deploy on Render

Use these settings:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`

## Notes

- Photos are shared in memory only.
- There is no login or account system.
- The app is designed to stay easy to run and deploy.
