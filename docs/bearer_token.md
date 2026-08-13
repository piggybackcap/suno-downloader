## Getting Your Bearer Token

The app uses your existing Suno browser session. No password is required.

1. Open **Chrome** or **Edge** and go to [suno.com](https://suno.com)
2. Make sure you are logged in
3. Open Developer Tools: **F12**
4. Go to the **Network** tab
5. Reload the page: **F5**
6. Type `feed` in the filter field
7. Click on the `v3` POST request
8. Under **Request Headers**, find the `Authorization` entry — it reads: `Bearer ey...`
9. Copy **only the part after `Bearer `** — the long `ey...` string

> Tokens expire after a few hours. If you get a 401 error, get a fresh token.

> Your token is like a password. The app stores it locally on your computer only.
