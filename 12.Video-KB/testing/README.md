# Testing

Everything needed to exercise the stack end to end without finding a real video.

| File | What it is |
|---|---|
| `talk.mp4` | 2m45s synthetic talk. Timecode and section name are burned into the picture, so a deep link is verifiable by eye. |
| `dummy_talk.txt` | The narration. Five clearly separated topics, so semantic search has something to discriminate. |
| `make_dummy_video.sh` | Regenerates `talk.mp4` from the narration (macOS `say` or `espeak-ng`, plus ffmpeg). |
| `demo_testing.html` | Search UI with an HLS player that seeks to the matched moment. |
| `serve.sh` | Serves this folder on `http://localhost:8000`. Required - see below. |
| `sample_questions.txt` | Questions with the timestamp each one should land on. |
| `sample_response.json` | A real captured `/search` response. |
| `sample_chunks.json` | The real chunk list the Distributed Map consumed, timestamps included. |

## Run it

```bash
# 1. ingest - the videos/ prefix is what the EventBridge rule listens on
aws s3 cp talk.mp4 s3://<MediaBucket>/videos/talk.mp4

# 2. wait for the pipeline (about 90s for this clip)
aws stepfunctions list-executions --state-machine-arn <PipelineArn> --max-items 1

# 3. open the demo
./serve.sh
```

Paste the stack's `SearchUrl` into the page, ask a question, and click a moment.
The player seeks there, and the burned-in timecode should match the moment's start.

## Do not open demo_testing.html from disk

Chrome sends no `Origin` header from a `file://` page, so CloudFront returns no CORS
header and hls.js cannot load the manifest. Search still works; playback does not.
`./serve.sh` gives the page an `http://` origin and everything works. The page shows a
banner if you forget.

## Re-ingesting

Uploading the same key again is safe. Vector keys are `{video_id}#{idx}`, so a re-ingest
overwrites the chunks instead of duplicating them. If you change the video but keep the
name, invalidate CloudFront (`/talk/*`) or you will keep seeing the old segments.


VideoKbStack.ApiEndpoint4F160690 = https://2s0cv3qwhi.execute-api.us-east-1.amazonaws.com/prod/
VideoKbStack.CdnDomain = d2s5i7anly7ef6.cloudfront.net
VideoKbStack.MediaBucket = videokbstack-mediaa721a567-mxuejdootrlr
VideoKbStack.PipelineArn = arn:aws:states:us-east-1:767398137682:stateMachine:video-kb-pipeline
VideoKbStack.SearchUrl = https://2s0cv3qwhi.execute-api.us-east-1.amazonaws.com/prod/search
VideoKbStack.VectorBucketName = video-kb-767398137682-us-east-1
VideoKbStack.VideosTable = VideoKbStack-Videos88AE8DA0-5K7YJRFVCYOV