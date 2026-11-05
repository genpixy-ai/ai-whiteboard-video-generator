---
name: genpixy
description: Make narrated explainer videos (MP4 with subtitles, a thumbnail, and a YouTube title) from a topic or a script with the GenPixy API. Use when the user asks for an explainer, educational, whiteboard, cartoon, or short video about a topic.
compatibility: Needs network access to www.genpixy.com, curl or another HTTP client, and a GenPixy API key in GENPIXY_API_KEY.
metadata:
  version: '1'
  docs: 'https://www.genpixy.com/api/docs.md'
  updated: '2026-11-03'
---

# GenPixy video API

GenPixy turns a topic or a script into a narrated explainer video. You start a video with one call, check it until it is done, and download the files.

Base URL: `https://www.genpixy.com/api/v1`. Every call needs the header `Authorization: Bearer $GENPIXY_API_KEY`.

If your client supports MCP, you can use the GenPixy MCP server instead: `https://www.genpixy.com/api/mcp` with the same Bearer header. Its tools (`create_video`, `get_video`, `list_videos`, `get_account`, `get_options`) follow the same steps as below.

## 1. Get the key

- Use the environment variable `GENPIXY_API_KEY`.
- If it is not set, ask the user: "Create a GenPixy API key at https://www.genpixy.com/account/api-keys and paste it here." Use the key only in this session unless the user asks you to save it.
- Never print the key, never write it into a file that goes into git, and never send it to any other site.

## 2. Check the balance and the cost

```sh
curl -s https://www.genpixy.com/api/v1/account -H "Authorization: Bearer $GENPIXY_API_KEY"
curl -s https://www.genpixy.com/api/v1/options -H "Authorization: Bearer $GENPIXY_API_KEY"
```

- Amounts look like `{"units": 16, "display": "…"}`. `display` is the exact text the user's app shows for this amount: show the user `display`. Compare `units` in code.
- Before a video longer than 2 minutes, or any 1080p video, tell the user its cost from `options.costs` and ask for a yes.

## 3. Start the video

```sh
curl -s https://www.genpixy.com/api/v1/videos \
  -H "Authorization: Bearer $GENPIXY_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: <one unique string per video, for example a UUID>" \
  -d '{"prompt":"How black holes form, for curious teenagers","seconds":60,"orientation":"landscape"}'
```

| Field                | Values                                                                                            | Default      |
| -------------------- | ------------------------------------------------------------------------------------------------- | ------------ |
| `prompt` (required)  | 3 to 5,000 characters: a topic, notes, or a full script. Line breaks and Markdown lists are fine. | —            |
| `seconds`            | 30, 60, 90, 120, 180, 300, 480, 600                                                               | 60           |
| `orientation`        | `landscape` (YouTube), `portrait` (TikTok, Reels, Shorts)                                         | `landscape`  |
| `resolution`         | `720p`, `1080p` (costs 1.5 times as much)                                                         | `720p`       |
| `style`              | `whiteboard`, `animate` (cartoon), `flat` (Color Board); `options.styles` lists what works now    | `whiteboard` |
| `language`           | `auto` (narration follows the prompt language), `en`, `id`                                        | `auto`       |
| `voice`              | `male`, `female`                                                                                  | `male`       |
| `subtitles`, `music` | `true`, `false`                                                                                   | `true`       |
| `big_board`          | `true`: all scenes on one board with a zoom-out recap at the end                                  | `false`      |
| `zoom_dive`          | `true`: the camera flies into an icon when the next scene looks closer at it                      | `false`      |

- If a call fails with a network error or an HTTP 5xx, send the same request again with the **same** `Idempotency-Key`. You get the same video back and pay only once.
- Answer 201 (new) or 200 (same key, same request): read `id` from the body.

## 4. Wait for it

```sh
curl -s https://www.genpixy.com/api/v1/videos/<id> -H "Authorization: Bearer $GENPIXY_API_KEY"
```

- Check every 10 seconds. Stop when `status` is `completed`, `failed`, or `canceled`.
- `progress` goes from 0 to 100. While `status` is `queued`, `queue_position` says how many videos start first. A video can go back to `queued` for a short time; keep checking.
- Tell the user about progress about once a minute, not after every check.
- `failed` and `canceled` videos cost nothing. Read `error.message` and tell the user.

## 5. Download

```sh
curl -fL -o video.mp4 "https://www.genpixy.com/api/v1/videos/<id>/content?variant=video" -H "Authorization: Bearer $GENPIXY_API_KEY"
curl -fL -o thumbnail.png "https://www.genpixy.com/api/v1/videos/<id>/content?variant=thumbnail" -H "Authorization: Bearer $GENPIXY_API_KEY"
curl -fL -o subtitles.srt "https://www.genpixy.com/api/v1/videos/<id>/content?variant=subtitles" -H "Authorization: Bearer $GENPIXY_API_KEY"
```

Give the user: the saved file paths, `result.title`, `result.youtube_title`, `result.youtube_description`, `result.hashtags`, and `app_url` (the video page in GenPixy). Files are kept for 60 days.

## Errors

Errors look like `{"error": {"code": "…", "message": "…", "request_id": "…"}}`. Tell the user `message`.

| HTTP | `code`                             | What to do                                                                          |
| ---- | ---------------------------------- | ----------------------------------------------------------------------------------- |
| 401  | `UNAUTHORIZED`                     | The key is wrong or revoked. Ask the user for a new key.                            |
| 402  | `INSUFFICIENT_BALANCE`             | Tell the user `needed.display` and `available.display`, and give them `top_up_url`. |
| 409  | `IDEMPOTENCY_KEY_REUSED`           | Use a new `Idempotency-Key` for a new video.                                        |
| 422  | `PROMPT_REFUSED`                   | This topic cannot be made. Suggest a different topic.                               |
| 429  | `TOO_MANY_ACTIVE_VIDEOS`           | 2 videos are in progress. Wait until one finishes.                                  |
| 429  | `RATE_LIMITED`                     | Wait `retry_after_seconds`, then try again.                                         |
| 503  | `TRY_AGAIN`, `SERVICE_UNAVAILABLE` | Wait 30 to 60 seconds, then retry once with the same `Idempotency-Key`.             |

## Writing a good prompt

- One clear topic, or a full script. Say who watches it and the tone: "for 10-year-olds", "for busy marketers, upbeat".
- Match the length to the content: 30 to 60 seconds for one idea, 2 to 3 minutes for a lesson, up to 10 minutes for a long script.
- Write the prompt in the language the narration should use, or set `language`.

More: full docs https://www.genpixy.com/api/docs.md · OpenAPI https://www.genpixy.com/api/openapi.json
