"""start_transcribe: kick off Transcribe with speaker labels.
   build_chunks:     Transcribe JSON → timestamped chunks (written to S3 for the Map)."""
import os
import json
import boto3

transcribe = boto3.client("transcribe")
s3 = boto3.client("s3")

MEDIA_BUCKET = os.environ["MEDIA_BUCKET"]
TRANSCRIPTS_BUCKET = os.environ["TRANSCRIPTS_BUCKET"]
CHUNK_SECONDS = float(os.environ.get("CHUNK_SECONDS", "45"))


def start_transcribe(event, context):
    key = event["key"]
    # The state machine derives video_id once and both branches share it.
    video_id = event.get("video_id") or key.split("/")[-1].split(".")[0]
    job = f"vkb-{video_id}-{context.aws_request_id[:8]}"
    transcribe.start_transcription_job(
        TranscriptionJobName=job,
        Media={"MediaFileUri": f"s3://{MEDIA_BUCKET}/{key}"},
        IdentifyLanguage=True,
        OutputBucketName=TRANSCRIPTS_BUCKET,
        OutputKey=f"{video_id}.json",
        Settings={"ShowSpeakerLabels": True, "MaxSpeakerLabels": 10},
    )
    return {"job_name": job, "video_id": video_id, "key": key}


def build_chunks(event, context):
    video_id = event["video_id"]
    data = json.loads(s3.get_object(
        Bucket=TRANSCRIPTS_BUCKET, Key=f"{video_id}.json")["Body"].read())
    items = data["results"]["items"]

    chunks, cur, start, last, idx = [], [], None, None, 0
    for it in items:
        if it["type"] == "pronunciation":
            t = float(it["start_time"])
            if start is None:
                start = t
            last = float(it["end_time"])
            cur.append(it["alternatives"][0]["content"])
            if t - start >= CHUNK_SECONDS:
                chunks.append({"video_id": video_id, "idx": idx,
                               "start": start, "end": last, "text": " ".join(cur)})
                idx += 1
                cur, start = [], None
        elif cur:
            cur.append(it["alternatives"][0]["content"])
    if cur and start is not None:
        chunks.append({"video_id": video_id, "idx": idx, "start": start,
                       "end": last if last is not None else start + CHUNK_SECONDS,
                       "text": " ".join(cur)})

    # Write chunks to S3 - the Distributed Map reads this file directly,
    # so we never push a big list through Step Functions state (256KB cap).
    key = f"chunks/{video_id}.json"
    s3.put_object(Bucket=TRANSCRIPTS_BUCKET, Key=key,
                  Body=json.dumps(chunks).encode(),
                  ContentType="application/json")
    return {"video_id": video_id, "chunks_key": key, "count": len(chunks)}
