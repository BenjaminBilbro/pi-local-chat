import json
import re

events = []

with open('custom_closure_capture.jsonl', 'r') as f:
    for line in f:
        events.append(json.loads(line))

print(json.dumps(events[7]['event'], indent=2))

# types: agent_start, turn_start, message_start (user/assistant), message_end (user/assistant), message_update (chunk, assistant)
# Note: event['event']['assistantMessageEvent']['partial'] == event['event']['message']

# Regex portion

EMOTION_TAGS = r'\[(?:laughter|sigh|confirmation-en|question-en|question-ah|question-oh|question-ei|question-yi|surprise-ah|surprise-oh|surprise-wa|surprise-yo|dissatisfaction-hnn)\]'
CODE_BLOCK = r'```[\s\S]*?```'
INLINE_CODE = r'`[^`]+`'
URLS = r'https?://\S+'
SENTENCE_BOUNDARY = r'(?<=[.!?])\s+(?=[A-Z])'
EMDASH_BOUNDARY = r'\s+[—–-]{2,}\s+'

BREAK_PATTERN = re.compile(f'({EMOTION_TAGS})|({SENTENCE_BOUNDARY})|({EMDASH_BOUNDARY})')


def _is_open(text):
    """True if buffer currently ends mid fence / mid inline-code / mid url."""
    if len(re.findall(r'```', text)) % 2 == 1:
        return True

    text_wo_blocks = re.sub(CODE_BLOCK, '', text)
    if text_wo_blocks.count('`') % 2 == 1:
        return True

    urls = list(re.finditer(URLS, text_wo_blocks))
    if urls and urls[-1].end() == len(text_wo_blocks):
        return True

    return False


def process_buffer(buffer):
    """
    Repeatedly strips completed code blocks and slices off completed chunks.
    Returns (chunks_to_emit, remaining_buffer).
    """
    chunks = []

    while True:
        if _is_open(buffer):
            break  # still mid-construct, wait for more tokens

        # Drop any completed code block entirely -- never emitted
        code_spans = [m.span() for m in re.finditer(CODE_BLOCK, buffer)]
        if code_spans:
            start, end = code_spans[0]
            buffer = buffer[:start] + buffer[end:]
            continue

        # Inline code / urls are kept in the text, just not split inside
        no_split_spans = (
            [m.span() for m in re.finditer(INLINE_CODE, buffer)]
            + [m.span() for m in re.finditer(URLS, buffer)]
        )

        match = next(
            (m for m in BREAK_PATTERN.finditer(buffer)
             if not any(s <= m.start() < e for s, e in no_split_spans)),
            None
        )
        if not match:
            break  # no valid break point yet

        if match.group(1):  # emotion tag -> keep tag, split right after it
            cut = match.end()
            chunk, buffer = buffer[:cut], buffer[cut:]
        else:  # sentence / em-dash boundary -> drop the whitespace, don't keep it either side
            chunk, buffer = buffer[:match.start()], buffer[match.end():]

        chunk = chunk.strip()
        if chunk:
            chunks.append(chunk)

    return chunks, buffer


parsed = []
buffer = ''
for idx, event in enumerate(events):
    if idx < 7:
        continue
    if event['event']['type'] == 'turn_end':
        break
    if 'assistantMessageEvent' in event['event'].keys():
        if event['event']['assistantMessageEvent']['type'] == 'text_delta':
            chunk = event['event']['assistantMessageEvent']['delta']
            buffer += chunk
            new_chunks, buffer = process_buffer(buffer)
            parsed.extend(new_chunks)

if buffer.strip():
    parsed.append(buffer.strip())

for idx, i in enumerate(parsed):
    print(f'Chunk {idx}: {i}')