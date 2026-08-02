# OmniVoice Voice Clone Prompt + Language Behavior Analysis

Research date: 2026-07-31
Scope: How voice clone prompts interact with language, instruct, and accents in OmniVoice, with implications for pi-chat's current implementation.

---

## Executive Summary

pi-chat's current bootstrap approach — generating English text with voice design, then creating a voice clone prompt from it — works for English but will produce cross-lingual accent artifacts for other languages.

**Key findings:**

1. The `language` parameter controls what language is spoken, but the voice clone prompt's accent characteristics bleed into the output.
2. The official docs explicitly warn: "For standard pronunciation, use a reference audio in the same language as the target speech."
3. When ref_audio and instruct conflict, "the model will most likely follow the style of the reference audio."
4. Voice design (instruct-only, no clone) is trained on Chinese and English only; it can generalize to other languages but "may produce unstable results for some low-resource languages or edge cases."
5. The docs warn that short 1–2 second clips without reference audio may be unreliable.

**Bottom line:** The bootstrap + clone approach is well-motivated for consistency, but needs language-aware bootstrapping for multi-language support.

---

## How Voice Clone Prompts Work

### What's Inside a VoiceClonePrompt

From `omnivoice/models/omnivoice.py` (line ~112):

```python
@dataclass
class VoiceClonePrompt:
    ref_audio_tokens: torch.Tensor  # (C, T)
    ref_text: str
    ref_rms: float
```

The voice clone prompt stores:
- **`ref_audio_tokens`**: Discrete audio tokens produced by the HiggsAudioV2TokenizerModel. These encode voice timbre, accent, prosody, and speech characteristics of the reference audio.
- **`ref_text`**: Transcript of the reference audio (used as conditioning text).
- **`ref_rms`**: Volume normalization factor.

### How create_voice_clone_prompt() Extracts Features

From `omnivoice/models/omnivoice.py` (line ~729):

1. Loads/reference audio at 24kHz sampling rate
2. Normalizes volume (RMS normalization)
3. Optionally preprocesses: trims long audio, removes silence
4. Auto-transcribes if ref_text not provided (via Whisper ASR)
5. Encodes audio through `self.audio_tokenizer.encode()` → discrete audio codes `(C, T)`
6. Returns VoiceClonePrompt with tokens, text, and RMS

**Key insight:** The audio tokens capture whatever characteristics are in the reference audio — including its language's phonetic patterns and accent. There is no explicit language tag stored in the prompt; the language is implicit in the audio tokens themselves.

### How generate() Combines voice_clone_prompt, language, and instruct

#### Style Token Construction

From `_prepare_inference_inputs()` (line ~1198):

```python
style_text = ""
if denoise and ref_audio_tokens is not None:
    style_text += "<|denoise|>"
lang_str = lang if lang else "None"
instruct_str = instruct if instruct else "None"
style_text += f"<|lang_start|>{lang_str}<|lang_end|>"
style_text += f"<|instruct_start|>{instruct_str}<|instruct_end|>"
```

#### Full Input Assembly

```python
# Conditional input is: style_tokens + text_tokens + [ref_audio_tokens] + target_audio_tokens
parts = [style_tokens, text_tokens]
if ref_audio_tokens is not None:
    parts.append(ref_audio_tokens.unsqueeze(0).to(self.device))
parts.append(target_audio_tokens)
cond_input_ids = torch.cat(parts, dim=2)
```

The model receives all three simultaneously:

| Signal | Source | Controls |
|--------|--------|----------|
| **`language`** | Style tokens via `<|lang_start|>XXX<|lang_end|>` | What language to speak (phoneme selection, grammar) |
| **`voice_clone_prompt`** | Ref audio tokens as conditioning | Voice timbre, accent, prosody, speaking style |
| **`instruct`** | Style tokens via `<|instruct_start|>XXX<|instruct_end|>` | Voice design attributes (gender, age, pitch, accent) |

**They are additive, not mutually exclusive.** The model conditions on all three at once.

---

## Official Documentation Findings

### Cross-Lingual Voice Cloning Warning

From `README.md`:

> "For standard pronunciation, use a reference audio in the **same language** as the target speech. In cross-lingual voice cloning (i.e., the reference audio and target speech are in different languages), the generated speech will carry an accent from the reference audio's language."

This is explicit: an English reference audio will impart an English accent to Spanish, French, Chinese, etc.

### Ref Audio vs Instruct Conflict

From `docs/tips.md`:

> When both `ref_audio` and `instruct` are provided and they **conflict**, the model will most likely follow the style of the reference audio. When the two are **consistent**, `instruct` can improve cloning stability for the attributes it describes.

Example: Providing dialect reference audio + matching dialect instruct for more stable output.

**Implication:** If the bootstrap instruct says "american accent" but the user wants "british accent", the reference audio (which was generated with "american accent") will likely override the instruct.

### Voice Design Training Data

From `README.md`:

> Voice design is trained on Chinese and English data only. It can generalize to other languages, but may produce unstable results for some low-resource languages or edge cases.

From `docs/voice-design.md`:

> The model is primarily trained on the voice cloning task, so voice cloning is the most stable mode.

**Implication:** Voice design (pure instruct) is less stable than voice cloning, especially for non-English/non-Chinese languages.

### Short Audio Warning

From `docs/tips.md`:

> The model may not reliably generate short audio clips (e.g., 1–2 seconds) without reference audio. If you need to generate short clips, provide reference audio to the model.

This is the core motivation for pi-chat's bootstrap approach.

### English Accent Limitations

From `docs/voice-design.md`:

> English Accent: Only effective when the synthesis text is in English.

Similarly, Chinese dialects only apply to Chinese speech.

**Implication:** The `accent` setting is language-scoped. "British accent" means British English, not British-accented Spanish.

---

## Current pi-chat Implementation Analysis

### The Bootstrap Flow

From `pi_chat/tts_service.py`:

1. `BOOTSTRAP_TEXT = "Hello. I'm ready to help with what you're working on today."` (English)
2. `prepare_voice()`:
   - Builds instruct from settings: `gender, age, pitch, accent, style`
   - Calls `model.generate(text=BOOTSTRAP_TEXT, instruct=instruct)` — **no language param**
   - Takes result audio → `model.create_voice_clone_prompt(waveform, ref_text=BOOTSTRAP_TEXT)`
   - Returns `{voice_clone_prompt, instruct}`
3. `generate()`:
   - Uses `voice_clone_prompt` + `instruct` + `language=settings.language`

### VoiceSettings Structure

From `tests/fakes/voice.py`:

```python
@dataclass(frozen=True)
class VoiceSettings:
    gender: str = "female"
    age: str = "young adult"
    pitch: str = "moderate pitch"
    accent: str = "american accent"
    style: str | None = None
    speed: float = 1.0
    language: str = "English"
```

### Problematic Scenarios

| User Settings | Bootstrap | Clone Prompt | Generation | Result |
|---------------|-----------|--------------|------------|--------|
| English text, american accent | English + "american accent" | English audio tokens | language="English", instruct="american accent" | ✅ Good match |
| English text, british accent | English + "american accent" | English audio tokens (american) | language="English", instruct="british accent" | ⚠️ Conflict — ref audio wins per docs |
| Spanish text, any accent | English + "american accent" | English audio tokens | language="Spanish", instruct="..." | ⚠️ Spanish with English accent |
| French text | English + "american accent" | English audio tokens | language="French" | ⚠️ French with English accent |
| Chinese text | English + "american accent" | English audio tokens | language="Chinese" | ⚠️ Chinese with English accent |
| Japanese text | English + "american accent" | English audio tokens | language="Japanese" | ⚠️ Japanese with English accent |

**The language parameter DOES control what language is spoken**, but the clone prompt's accent/timbre from the English bootstrap bleeds through.

---

## Voice Design Without Cloning: Would It Work?

### The Question

What if we skip the bootstrap clone entirely and just use instruct-only generation for every chunk?

### What the Docs Say

**Pros:**
- No accent bleed from a mismatched reference language
- Aligns with the demo's "Voice Design" mode
- Simpler implementation

**Cons:**
- "Voice design is trained on Chinese and English data only" — less stable for other languages
- "The model is primarily trained on the voice cloning task, so voice cloning is the most stable mode"
- "The model may not reliably generate short audio clips (e.g., 1–2 seconds) without reference audio"
- pi-chat's VOICE_MODE_IMPLEMENTATION_PLAN_V3.md explicitly warns: "Voice design on many short independent chunks can drift."

### Drift Analysis

The drift concern is real but nuanced:

1. **Within a single voice design call** (long text): The model handles this via built-in chunking (`audio_chunk_duration=15.0` default). Same instruct, same generation context.

2. **Across multiple independent voice design calls** (pi-chat's use case): Each call is a fresh generation with the same instruct. The model should produce consistent output for the same instruct string, but:
   - Randomness in sampling (`position_temperature=5.0` default)
   - No shared conditioning between calls
   - Very short chunks (1-3 seconds) may be "unreliable without reference audio"

The drift is likely more subtle than catastrophic — small variations in timbre, pitch, or energy between chunks rather than the voice completely changing. For a chat assistant reading short responses, this could be noticeable but not deal-breaking.

### Verdict on Pure Voice Design

- **For English/Chinese:** Likely acceptable for short chunks, but bootstrap+clone is more stable per the docs.
- **For other languages:** May produce "unstable results" per the docs. Bootstrap+clone with a same-language reference is better.
- **For pi-chat's streaming use case:** The drift concern is legitimate. Multiple short independent calls without a reference will have more variation than cloned calls.

---

## Accent Handling Deep Dive

### English Accents

From `docs/voice-design.md`, supported English accents:
- american accent, british accent, australian accent, canadian accent, indian accent, chinese accent, korean accent, japanese accent, portuguese accent, russian accent

**Only effective for English speech.** These mean "X-accented English", not "X accent applied to any language."

### Cross-Language Accent Behavior

The docs don't explicitly describe what happens when you:
- Generate Spanish with a "british accent" instruct
- Generate Spanish with an English reference audio

From the cross-lingual cloning warning, we know the reference audio's accent bleeds through. The instruct's accent attribute is likely ignored for non-English speech (per the "only effective for English" note).

### Current Implementation Issue

pi-chat's bootstrap uses `accent="american accent"` by default. This means:
- All voice clone prompts have American-accented English audio tokens
- For English speech: this matches the default user expectation
- For non-English speech: the American accent bleeds through
- For English speech with non-american accent selected: conflict (ref audio wins)

---

## Language Resolution in OmniVoice

From `_preprocess_all()` (line ~1024), language is resolved via `_resolve_language()`:

```python
language_list = [_resolve_language(lang) for lang in language_list]
```

The `language` parameter accepts:
- Language name (e.g., "English", "Spanish")
- Language code (e.g., "en", "es")
- None for language-agnostic mode

From `omnivoice/utils/lang_map.py`, there are 600+ supported languages via `LANG_IDS` and `LANG_NAMES`.

**Important:** The bootstrap generation in pi-chat does NOT pass a language parameter. It relies on auto-detection from the English BOOTSTRAP_TEXT. This works but is implicit.

---

## Practical Recommendations for pi-chat

### Option A: Language-Specific Bootstrap Prompts (Recommended)

Create separate bootstrap prompts keyed by `{voice_settings, language}`.

**Implementation:**
- Add language-specific bootstrap texts for major language groups
- When `prepare_voice()` is called, use the bootstrap text matching the requested language
- Cache by `{settings_hash, language}` instead of just `{settings_hash}`

**Example bootstrap texts:**
- English: "Hello. I'm ready to help with what you're working on today."
- Spanish: "Hola. Estoy lista para ayudarte con lo que estés trabajando hoy."
- French: "Bonjour. Je suis prête à vous aider avec ce sur quoi vous travaillez aujourd'hui."
- Chinese: "你好。我准备好帮助你今天的工作了。"
- Japanese: "こんにちは。今日はどんなお手伝いしましょうか。"

**Pros:**
- Keeps consistency benefits of cloning
- Matches official doc recommendation (same language reference)
- No accent bleed for non-English languages
- Handles accent conflicts for English (british accent bootstrap for british accent setting)

**Cons:**
- More VRAM for cached prompts (each language variant is separate)
- Extra bootstrap latency on first use of each language
- Need to maintain bootstrap texts for supported languages

**Variant:** Only do this for languages where the user explicitly changes language. Default English bootstrap for English text.

### Option B: Pure Voice Design for Non-English Languages

- For English: use current bootstrap + clone approach
- For other languages: skip clone prompt, use instruct-only generation

**Pros:**
- No accent bleed for non-English
- Simpler than maintaining language-specific bootstraps

**Cons:**
- Less stable per the docs ("voice cloning is the most stable mode")
- Short chunk unreliability for non-English
- Potential drift across chunks
- Inconsistent behavior between English and other languages

### Option C: Hybrid — Detect Language, Choose Strategy

- If language is English/Chinese (voice design training languages):
  - Use instruct-only generation (no bootstrap needed)
- If language is anything else:
  - Generate a same-language bootstrap, create clone prompt, use for subsequent chunks

**Pros:**
- Optimizes for known-stable languages
- Handles edge cases for low-resource languages

**Cons:**
- Most complex option
- Still has drift concern for English/Chinese short chunks

### Option D: Accent-Aware English Bootstrap

Keep current approach but make the bootstrap accent match the user's selected accent for English speech.

**Implementation:**
- Bootstrap includes the selected accent in its instruct
- For non-English languages, accept the accent bleed or add language-specific bootstraps

**Pros:**
- Fixes the English accent conflict issue
- Minimal change from current implementation

**Cons:**
- Doesn't fix non-English accent bleed
- Still generates English bootstrap for non-English speech

---

## Summary of Key Code References

| File | Location | Relevant Code |
|------|----------|---------------|
| `omnivoice/models/omnivoice.py` | Line ~112 | VoiceClonePrompt dataclass |
| `omnivoice/models/omnivoice.py` | Line ~729 | create_voice_clone_prompt() |
| `omnivoice/models/omnivoice.py` | Line ~584 | generate() signature and modes |
| `omnivoice/models/omnivoice.py` | Line ~1024 | _preprocess_all() language/instruct resolution |
| `omnivoice/models/omnivoice.py` | Line ~1198 | _prepare_inference_inputs() style token construction |
| `README.md` | Voice Cloning tips | Cross-lingual accent warning |
| `docs/tips.md` | Combination section | Ref audio vs instruct conflict |
| `docs/tips.md` | Short Audio section | Short clip reliability warning |
| `docs/voice-design.md` | Intro | Training data limitations |
| `pi_chat/tts_service.py` | Line ~37 | BOOTSTRAP_TEXT constant |
| `pi_chat/tts_service.py` | Line ~190 | prepare_voice() bootstrap flow |
| `pi_chat/tts_service.py` | Line ~230 | generate() with voice_clone_prompt |
| `tests/fakes/voice.py` | Line ~33 | VoiceSettings dataclass |

---

## Conclusion

The current implementation is well-motivated (stability for short chunks) but has a real multi-language flaw. The recommended fix is **Option A: language-specific bootstrap prompts**, which:

1. Aligns with official documentation guidance
2. Preserves the stability benefits of cloning
3. Eliminates cross-lingual accent artifacts
4. Is backward-compatible with English-first usage

The pure voice design alternative (Option B) is tempting for simplicity but contradicts the docs' explicit warnings about short clips and stability.
