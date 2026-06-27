import csv
import os
import pickle
import random
import socket
import time
import uuid
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from atproto import Client
from atproto_client.utils import TextBuilder

# Unique tag for this process. Used to "claim" a video file in Drive before
# downloading/posting it, so that two workflow runs (e.g. two GitHub
# accounts/workflows pointed at the same Drive folder) can't both grab the
# same file at the same time. GITHUB_RUN_ID is stable for the life of one
# workflow run, which is exactly the scope we want a claim to last for.
RUN_TAG = os.getenv("GITHUB_RUN_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
CLAIM_PREFIX = "CLAIMED_"


def get_env(name, required=True):
    """Read an env var and strip surrounding whitespace/newlines.

    GitHub Actions secrets occasionally end up with a trailing newline if
    they were copy/pasted from a file or terminal. That trailing \n gets
    silently included in API calls (e.g. Drive folder IDs), causing
    confusing 404s like "File not found: ." since the ID no longer matches
    anything. Stripping here makes the script robust to that.
    """
    value = os.getenv(name)
    if value is None:
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return ""
    return value.strip()


def get_creds():
    """Load token.pickle from repo root, refreshing the access token if it has expired."""
    with open("token.pickle", "rb") as token:
        creds = pickle.load(token)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def load_hashtag_sets(filepath="hashtags.txt"):
    """Return a list of hashtag sets (one per non-empty line)."""
    sets = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                sets.append(line)
    return sets


def pick_random_hashtags(filepath="hashtags.txt"):
    """Pick one random hashtag set; return list of tags without the # prefix."""
    hashtag_sets = load_hashtag_sets(filepath)
    if not hashtag_sets:
        return []
    chosen_line = random.choice(hashtag_sets)
    return [word.lstrip("#") for word in chosen_line.split() if word.startswith("#")]


def load_caption_rows(filepath="recipes_captions.csv"):
    """Return a list of (caption, link_action_caption) tuples from the CSV.

    Each row pairs a caption with its own matching CTA text, so we keep
    them together rather than picking each independently — that's what
    keeps the CTA relevant to the caption it's paired with.
    """
    rows = []
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        # Be tolerant of the "lik_action_caption" header typo in the source
        # file, or a correctly spelled "link_action_caption" header.
        for row in reader:
            caption = (row.get("captions") or "").strip()
            cta = (
                row.get("link_action_caption")
                or row.get("lik_action_caption")
                or ""
            ).strip()
            if caption and cta:
                rows.append((caption, cta))
    return rows


def pick_random_caption_and_cta(filepath="recipes_captions.csv"):
    """Pick one random (caption, cta) pair; return ('', '') if none found."""
    rows = load_caption_rows(filepath)
    if not rows:
        return "", ""
    return random.choice(rows)


def claim_file(service, file_id, current_name):
    """
    Try to "claim" a Drive file by renaming it with this run's unique tag.

    Drive has no real locking primitive, so we approximate one: rename is a
    single atomic write, and we immediately re-fetch the file to confirm our
    rename is still in effect. If a different concurrent run (e.g. a second
    GitHub Actions workflow polling the same folder) renamed the file in the
    moment between our list() and our update(), the re-fetch will show their
    tag instead of ours and we back off rather than risk a double-post.

    Returns the new (claimed) filename on success, or None if we lost the race.
    """
    claimed_name = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    service.files().update(
        fileId=file_id,
        body={"name": claimed_name},
    ).execute()

    # Re-fetch to make sure nobody else won/overwrote the claim in between.
    check = service.files().get(fileId=file_id, fields="id, name").execute()
    if check.get("name") != claimed_name:
        print(f"Lost claim race on file {file_id} (now named '{check.get('name')}'); skipping.")
        return None
    return claimed_name


def fetch_latest_video():
    creds = get_creds()
    service = build("drive", "v3", credentials=creds)
    folder_id = get_env("UPLOAD_FOLDER_ID")
    results = service.files().list(
        q=f"'{folder_id}' in parents",
        orderBy="createdTime desc",
        pageSize=10
    ).execute()
    files = results.get("files", [])
    if not files:
        print("No files found in upload folder.")
        return None, None

    for file in files:
        mime_type = file.get("mimeType", "")
        original_name = file["name"]

        # Skip files already claimed by another concurrent run (e.g. the
        # other GitHub account's workflow grabbed it first and hasn't
        # finished posting/moving it yet).
        if original_name.startswith(CLAIM_PREFIX):
            print(f"Skipping '{original_name}' — already claimed by another run.")
            continue

        print(f"Found file: {original_name} ({mime_type})")
        if not mime_type.startswith("video/"):
            continue

        claimed_name = claim_file(service, file["id"], original_name)
        if claimed_name is None:
            # Another run claimed it first; move on to the next candidate
            # instead of giving up entirely for this cycle.
            continue

        print(f"Claimed '{original_name}' as '{claimed_name}'.")
        request = service.files().get_media(fileId=file["id"])
        local_path = f"/tmp/{original_name}"
        with open(local_path, "wb") as f:
            f.write(request.execute())

        # Keep the original display name for the Bluesky alt text / logs,
        # but remember the file's id and claimed name for the move step.
        file["claimed_name"] = claimed_name
        file["original_name"] = original_name
        return file, local_path

    print("No unclaimed video files found in upload folder.")
    return None, None


def move_file(file_id, restore_name=None):
    creds = get_creds()
    service = build("drive", "v3", credentials=creds)
    upload_id = get_env("UPLOAD_FOLDER_ID")
    processed_id = get_env("PROCESSED_FOLDER_ID")

    body = {}
    if restore_name:
        # Drop the CLAIMED_<run>__ prefix once we're safely done with the
        # file, so the processed folder shows clean original filenames.
        body["name"] = restore_name

    service.files().update(
        fileId=file_id,
        addParents=processed_id,
        removeParents=upload_id,
        body=body,
    ).execute()
    print("Moved file to processed folder.")


MAX_POST_LENGTH = 300  # Bluesky's grapheme limit per post
LOOP_INTERVAL_SECONDS = 4600  # 60 minutes between cycles

# ── Link definition ───────────────────────────────────────────────────────
# Bluesky shows a "Leaving Bluesky" confirmation interstitial whenever the
# displayed link text doesn't match the href's domain (phishing protection).
# To get a plain clickable link that opens directly with no warning, the
# *displayed* text must be exactly the bare domain — same text Bluesky's own
# UI would render for a link facet pointing at that domain.
LINK_URL = "https://boobs.teentoday.cfd"
LINK_DISPLAY_TEXT = "boobs.teentoday.cfd"


def build_post(tags: list[str]) -> TextBuilder:
    """
    Final post layout:

        Caption line
        \n
        <link_action_caption from the same CSV row>
        bnn.teentoday.cfd   (clickable link, opens with no warning)
        \n
        #tag1 #tag2 #tag3 ...
    """
    tb = TextBuilder()

    caption, cta = pick_random_caption_and_cta("recipes_captions.csv")
    if caption:
        tb.text(caption)
        tb.text("\n\n")

    # Plain-text CTA line (matched to the caption above from the same CSV
    # row), then the clickable domain link on the line below it. Display
    # text == bare domain == href domain, so Bluesky opens it directly
    # instead of showing the leaving-site warning.
    if cta:
        tb.text(cta)
        tb.text("\n")
    tb.link(LINK_DISPLAY_TEXT, LINK_URL)
    tb.text("\n\n")

    for i, tag in enumerate(tags):
        tb.tag(f"#{tag}", tag)
        if i < len(tags) - 1:
            tb.text(" ")

    return tb


def post_to_bluesky(video_name, local_path):
    handle = get_env("BSKY_HANDLE")
    app_pw = get_env("BSKY_APP_PW")
    client = Client()
    client.login(handle, app_pw)

    with open(local_path, "rb") as f:
        video_bytes = f.read()

    tags = pick_random_hashtags("hashtags.txt")
    text_builder = build_post(tags)

    client.send_video(
        text=text_builder,
        video=video_bytes,
        video_alt=video_name,
    )
    print("Posted to Bluesky:")
    print("  Link:", LINK_DISPLAY_TEXT)
    print("  Tags:", " ".join(f"#{t}" for t in tags))


def release_claim(file_id, original_name):
    """
    Rename a claimed file back to its original name if something failed
    after claiming but before the move-to-processed step. Without this, a
    failed post would leave the file stuck with a CLAIMED_ prefix forever,
    invisible to future fetch_latest_video() calls.
    """
    try:
        creds = get_creds()
        service = build("drive", "v3", credentials=creds)
        service.files().update(fileId=file_id, body={"name": original_name}).execute()
        print(f"Released claim on '{original_name}' after failure.")
    except Exception as e:
        print(f"Warning: failed to release claim on file {file_id}: {e}")


def run_once():
    """Run a single fetch -> post -> move cycle."""
    file, local_path = fetch_latest_video()
    if not file:
        print("No new video this cycle.")
        return

    original_name = file.get("original_name", file["name"])
    try:
        post_to_bluesky(original_name, local_path)
    except Exception:
        # Posting failed (e.g. transient API error) — give the file back so
        # it's eligible to be picked up and retried next cycle, rather than
        # leaving it stuck under a CLAIMED_ name indefinitely.
        release_claim(file["id"], original_name)
        raise

    move_file(file["id"], restore_name=original_name)
    # Clean up the local temp copy so disk doesn't fill up over a long-running loop
    try:
        os.remove(local_path)
    except OSError:
        pass


def main():
    """
    Loop forever, running one post cycle every LOOP_INTERVAL_SECONDS.
    Each cycle is wrapped in try/except so a single failure (e.g. a transient
    API error) doesn't kill the whole loop - it just gets logged and retried
    next cycle.
    """
    print(f"Starting loop. Posting every {LOOP_INTERVAL_SECONDS} seconds.")
    while True:
        cycle_start = time.time()
        try:
            run_once()
        except Exception as e:
            print(f"Error during cycle: {e}")

        elapsed = time.time() - cycle_start
        sleep_for = max(0, LOOP_INTERVAL_SECONDS - elapsed)
        print(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.1f}s...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
