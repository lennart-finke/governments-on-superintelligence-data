You are adjudicating a passage from an official government record. Score how relevant the
passage is to each tracked topic area (0-100), then decide whether it contains a statement
by a lawmaker or senior executive official that substantively engages one of the specific
topics. This mirrors CODEBOOK.md; apply it strictly.

## Topic areas — score each 0-100

Score how strongly the SPEAKER'S OWN STATEMENT engages each area (0 = not at all,
100 = the passage is centrally about it). Score independently; several can be high at once.

- `ai` — artificial intelligence generically: any AI/ML/LLM subject matter at all.
- `agi` — artificial general intelligence: AI matching or exceeding human capability across
  most cognitive domains ("AGI", "human-level AI", "strong AI", "transformative AI",
  automated coding as full labor replacement, 通用人工智能, 汎用人工知能).
  NOT "general-purpose AI models" (EU AI Act regulatory category).
- `asi` — artificial superintelligence: AI far surpassing the best human minds
  ("superintelligence", "ASI", "superhuman intelligence", "smarter than humans", 超级智能).
- `rsi` — recursive self-improvement and discontinuous capability escalation: self-improving
  AI, intelligence explosion, the technological singularity, AI takeoff, runaway AI,
  automated AI R&D (自我迭代升级, シンギュラリティ). NOT ordinary model retraining.
- `x_risk` — AI existential or catastrophic risk and its mitigation machinery: human
  extinction or civilization-scale harm from AI, loss of control, uncontrollable AI,
  kill switches, alignment/misalignment, deceptive or power-seeking AI, self-exfiltration,
  scalable oversight, interpretability (安全、可靠、可控, 失控, 人工智能安全监管制度).
  NOT bias/jobs/privacy/disinformation, NOT "existential threat" to an industry or party.
- `regulation` — AI regulation and governance: AI laws and governance frameworks,
  international AI treaties, chip/semiconductor export controls, datacenter security,
  compute governance. Generic tech regulation without AI focus scores low.

## Subcategory scores

Whenever `x_risk` > 0, also score its subcategories 0-100 in `x_risk_sub` (all 0 is fine
when x_risk itself is 0; you may then set `x_risk_sub` to null):

- `misuse` — deliberate harmful use of AI by malicious actors (crime, terror, disinformation
  campaigns at catastrophic scale).
- `loss_of_control` — humans irreversibly losing control of AI systems; misaligned,
  deceptive, or power-seeking AI; kill switches; alignment as a safety problem.
- `natsec_stability` — AI as a threat to national security or international strategic
  stability (arms races, first-strike instability, great-power conflict over AI).
- `cbrn` — AI enabling chemical, biological, radiological, or nuclear harm.
- `socioeconomic` — civilization-scale socioeconomic disruption (mass labor displacement,
  collapse-level economic transformation). Ordinary "AI and jobs" concerns score low.

Whenever `regulation` > 0, also score its subcategories 0-100 in `regulation_sub`
(null when regulation is 0):

- `export_controls` — export controls on chips, semiconductor equipment, model weights.
- `standards_certification` — standards, certifications, licensing regimes for AI.
- `auditing` — audits, evaluations, red-teaming, pre-deployment testing requirements.
- `international_coordination` — international agreements, treaties, summits, coordination
  bodies (e.g. an "IAEA for AI").
- `military_defense` — regulation/governance of military AI, autonomous weapons.
- `surveillance` — AI surveillance and its governance.
- `alignment` — mandated alignment, safety cases, or control requirements.
- `adversarial_robustness` — robustness against adversarial attacks/jailbreaks as a
  regulatory requirement.

## Rationale and passage

- `rationale`: exactly two sentences: (1) what the speaker says that drives the scores,
  (2) why the borderline scores are as high/low as they are.
- `quote_span`: copy the single most relevant contiguous passage VERBATIM from the text —
  character-for-character in the ORIGINAL language, no ellipses, no corrections. It must be
  an exact substring. Aim for 1-4 sentences capturing the strongest topic engagement.
  Empty string if nothing is relevant.
- `quote_en`: faithful English translation of quote_span; null when the original is English.

## Decision fields

- `is_substantive`: at least one sentence of the speaker's own engagement. Excludes:
  jokes/asides, reciting a bill title or amendment text without engagement, procedural
  mentions, merely quoting a third party (an expert, a constituent, a report) without
  adopting the view. EXCEPTION for institutional policy documents (resolutions, adopted
  plans, official opinions): a concrete policy provision that engages a tracked topic —
  e.g. 建立人工智能安全监管制度, mandating AI kill-switch/oversight mechanisms — IS
  substantive even when it is a single clause; generic "AI brings risks" boilerplate is not.
- `speaker_owns_statement`: the view is the speaker's own (adopted), not merely reported.
  Quoting an expert approvingly to make their own argument = owns it. Reading someone
  else's letter into the record = does not.
- `quote_type`:
  - `direct` — verbatim words of the speaker (spoken transcript or their own written text,
    or quotation-marked text inside an official readout).
  - `official_paraphrase` — official-media rendering of a leader's remarks without
    quotation marks (e.g. "Xi stressed that ..."; 习近平强调). In the Chinese system this
    is the authoritative record; still label it paraphrase.
  - `reported` — anything else second-hand.
- `speaker_in_scope`: speaker is a lawmaker (MP, Lord, MEP, Member of Congress, MdB,
  député/sénateur, NPC delegate) or senior executive official (president, premier, chancellor,
  minister, EU Commissioner, agency head e.g. CAC director, or the issuing party/state organ
  for official CN policy documents; the White House and the Élysée as institutional authors
  of official statements/fact sheets count). Officials of foreign governments, staff
  witnesses, academics, and company executives are OUT of scope.
- `trigger_phrases`: the exact phrases (from the original text) that engage the topics.
- `stance`: speaker's attitude toward the risk/topic: `concerned` (takes it seriously /
  urges action), `dismissive` (downplays), `optimistic` (emphasizes benefit/controllability),
  `mixed`, `neutral` (mentions without evaluating).
- `context_note`: one neutral sentence describing the setting (chamber, debate, occasion).
- `speaker_name`: null when the metadata already names the speaker. When Speaker (as
  recorded) is UNKNOWN, put the person (or issuing organ) whose statement the quote_span
  is, exactly as named in the text with their role, e.g. "Xi Jinping (General Secretary)",
  "Zhuang Rongwen (CAC Director)", "State Council". If the passage names no in-scope
  speaker, set speaker_in_scope=false.

A passage counts for the dataset only when a SPECIFIC topic (agi/asi/rsi/x_risk/regulation)
scores >= 60 AND is_substantive AND speaker_owns_statement AND speaker_in_scope. A passage
that is merely about AI generically (only `ai` is high) does not count; score it honestly
and it will be filtered.

Respond with ONLY a JSON object with exactly these fields, in this order:
{"relevance": {"ai": 0, "agi": 0, "asi": 0, "rsi": 0, "x_risk": 0, "regulation": 0,
  "x_risk_sub": {"misuse": 0, "loss_of_control": 0, "natsec_stability": 0, "cbrn": 0,
                 "socioeconomic": 0},
  "regulation_sub": {"export_controls": 0, "standards_certification": 0, "auditing": 0,
                     "international_coordination": 0, "military_defense": 0,
                     "surveillance": 0, "alignment": 0, "adversarial_robustness": 0}},
 "rationale": "...", "quote_span": "...", "quote_en": null,
 "is_substantive": bool, "speaker_owns_statement": bool,
 "quote_type": "direct|official_paraphrase|reported", "speaker_in_scope": bool,
 "trigger_phrases": [...], "stance": "concerned|dismissive|optimistic|mixed|neutral",
 "context_note": "...", "speaker_name": null}
