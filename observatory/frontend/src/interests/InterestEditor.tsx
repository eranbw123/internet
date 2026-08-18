/** The interest editor: one save, no PR, no hand-run DB op.
 *
 * The same form serves three jobs, because they are the same form:
 *   - editing an existing interest,
 *   - creating one from scratch,
 *   - editing an offer before accepting it (the "Edit and accept" path).
 * Only the key field and the save button's wording differ.
 *
 * The field worth the most here is the bar. It is the single number that
 * decides whether an interest delivers anything, it was retuned by hand across
 * all 33 interests on 2026-08-13, and until now there was no way to see the
 * consequence of a change before making it. So the bar carries a live preview
 * -- "at 0.72, 15 of the last 72 scored items would clear" -- counted from the
 * interest's own recent scores as the slider moves, with no round-trip. That
 * turns bar-tuning from guesswork into a reading.
 */
import { useEffect, useMemo, useState } from "react";
import type { InterestDetailResponse, InterestPayload, InterestStat, Lifecycle, Offer } from "./types";
import { KNOWN_SOURCES, barPreview, coerceThreshold, validateInterest } from "./validation";
import { BidiText, guessLang } from "./Bidi";

export type EditorSubject =
  /** `detail` is loaded from the existing GET /observatory/api/interest/<key>
   * BEFORE the editor opens. The bulk stats payload carries no description or
   * signal arrays, and opening the editor without them would show empty
   * fields that a save would then write back as empty -- silently wiping the
   * signals that make the interest match anything. */
  | { mode: "edit"; interest: InterestStat; detail: InterestDetailResponse }
  | { mode: "create" }
  | { mode: "offer"; offer: Offer };

interface Props {
  subject: EditorSubject;
  /** The interest's own recent final_score values, for the bar preview. */
  recentScores: number[];
  /** For key-collision validation on create. */
  existingKeys: string[];
  /** For the parent picker. */
  parentOptions: { key: string; title: string }[];
  saving: boolean;
  error: string | null;
  onCancel: () => void;
  onSave: (payload: InterestPayload) => void;
}

function initialState(subject: EditorSubject) {
  if (subject.mode === "edit") {
    const i = subject.interest;
    const d = subject.detail;
    return {
      key: i.key,
      title: d.definition.title || i.title,
      description: d.definition.description ?? "",
      positive_signals: [...d.signals.positive],
      negative_signals: [...d.signals.negative],
      min_score_raw: i.min_score.toFixed(2),
      sources: [...i.sources],
      parent_key: i.parent_key,
      lifecycle: i.lifecycle as Lifecycle,
    };
  }
  if (subject.mode === "offer") {
    const o = subject.offer;
    return {
      key: o.key,
      title: o.title,
      description: o.description,
      positive_signals: [...o.positive_signals],
      negative_signals: [...o.negative_signals],
      min_score_raw: (o.suggested_min_score ?? 0.7).toFixed(2),
      sources: [...o.suggested_sources],
      parent_key: o.parent_key,
      lifecycle: "active" as Lifecycle,
    };
  }
  return {
    key: "",
    title: "",
    description: "",
    positive_signals: [] as string[],
    negative_signals: [] as string[],
    min_score_raw: "0.72",
    sources: ["web_search"],
    parent_key: null as string | null,
    lifecycle: "active" as Lifecycle,
  };
}

function SignalChips({
  label, hint, values, onChange, error,
}: {
  label: string; hint: string; values: string[];
  onChange: (next: string[]) => void; error?: string;
}) {
  const [draft, setDraft] = useState("");
  function add() {
    const v = draft.trim();
    if (!v || values.includes(v)) { setDraft(""); return; }
    onChange([...values, v]);
    setDraft("");
  }
  return (
    <div className={`field ${error ? "field-invalid" : ""}`}>
      <label className="field-label">{label}<span className="field-hint">{hint}</span></label>
      <div className="chip-input">
        {values.map((v) => (
          <span className="signal-chip" key={v}>
            <BidiText lang={guessLang(v)}>{v}</BidiText>
            <button
              type="button" aria-label={`Remove ${v}`}
              onClick={() => onChange(values.filter((x) => x !== v))}
            >
              x
            </button>
          </span>
        ))}
        <input
          value={draft}
          placeholder="add a signal"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); add(); }
            if (e.key === "Backspace" && !draft && values.length) onChange(values.slice(0, -1));
          }}
          onBlur={add}
          aria-label={label}
        />
      </div>
      {error && <p className="field-error">{error}</p>}
    </div>
  );
}

export function InterestEditor({
  subject, recentScores, existingKeys, parentOptions, saving, error, onCancel, onSave,
}: Props) {
  const [form, setForm] = useState(() => initialState(subject));
  useEffect(() => { setForm(initialState(subject)); }, [subject]);

  const parsedBar = Number(form.min_score_raw);
  const effectiveBar = Number.isFinite(parsedBar) ? coerceThreshold(parsedBar) : NaN;

  const result = useMemo(
    () => validateInterest(
      {
        key: form.key,
        title: form.title,
        description: form.description,
        positive_signals: form.positive_signals,
        negative_signals: form.negative_signals,
        sources: form.sources,
        parent_key: form.parent_key,
        lifecycle: form.lifecycle,
        min_score_raw: form.min_score_raw,
      },
      { existingKeys, isNew: subject.mode === "create" },
    ),
    [form, existingKeys, subject.mode],
  );

  const preview = Number.isFinite(effectiveBar)
    ? barPreview(recentScores, effectiveBar)
    : { clears: 0, of: recentScores.length };

  function set<K extends keyof typeof form>(k: K, v: (typeof form)[K]) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!result.ok || saving) return;
    onSave({
      key: form.key.trim(),
      title: form.title.trim(),
      description: form.description,
      positive_signals: form.positive_signals,
      negative_signals: form.negative_signals,
      min_score: effectiveBar,
      sources: form.sources,
      parent_key: form.parent_key || null,
      lifecycle: form.lifecycle,
    });
  }

  const heading = subject.mode === "create"
    ? "New interest"
    : subject.mode === "offer"
      ? `Edit before accepting - ${subject.offer.key}`
      : `Edit interest - ${subject.interest.key}`;

  return (
    <div className="editor-scrim" onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <form
        className="editor" onSubmit={submit} data-testid="interest-editor"
        role="dialog" aria-modal="true" aria-label={heading}
      >
        <header className="editor-head">
          <h2>{heading}</h2>
          <button type="button" className="btn btn-quiet" onClick={onCancel}>Close</button>
        </header>

        <div className="editor-body">
          {subject.mode === "create" && (
            <div className={`field ${result.errors.key ? "field-invalid" : ""}`}>
              <label className="field-label" htmlFor="ed-key">
                Key<span className="field-hint">lowercase slug, permanent</span>
              </label>
              <input
                id="ed-key" value={form.key} autoFocus
                onChange={(e) => set("key", e.target.value)}
              />
              {result.errors.key && <p className="field-error">{result.errors.key}</p>}
            </div>
          )}

          <div className={`field ${result.errors.title ? "field-invalid" : ""}`}>
            <label className="field-label" htmlFor="ed-title">Title</label>
            <input id="ed-title" value={form.title} onChange={(e) => set("title", e.target.value)} />
            {result.errors.title && <p className="field-error">{result.errors.title}</p>}
          </div>

          <div className="field">
            <label className="field-label" htmlFor="ed-desc">
              Description<span className="field-hint">what the scorer is told to look for</span>
            </label>
            <textarea
              id="ed-desc" rows={5} value={form.description}
              onChange={(e) => set("description", e.target.value)}
            />
          </div>

          <SignalChips
            label="Positive signals" hint="what should match"
            values={form.positive_signals}
            onChange={(v) => set("positive_signals", v)}
            error={result.errors.positive_signals}
          />
          <SignalChips
            label="Negative signals" hint="what should not"
            values={form.negative_signals}
            onChange={(v) => set("negative_signals", v)}
          />

          <div className={`field field-bar ${result.errors.min_score ? "field-invalid" : ""}`}>
            <label className="field-label" htmlFor="ed-bar">
              Bar<span className="field-hint">final_score an item must clear to be delivered</span>
            </label>
            <div className="bar-row">
              <input
                id="ed-bar" type="range" min={0} max={1} step={0.01}
                value={Number.isFinite(effectiveBar) ? effectiveBar : 0.7}
                onChange={(e) => set("min_score_raw", e.target.value)}
                aria-describedby="ed-bar-preview"
              />
              <input
                className="bar-number" inputMode="decimal" value={form.min_score_raw}
                onChange={(e) => set("min_score_raw", e.target.value)}
                aria-label="Bar value"
              />
            </div>
            <p id="ed-bar-preview" className="bar-preview">
              {recentScores.length === 0 ? (
                <span className="prov-muted">
                  No scored items yet, so there is nothing to preview against.
                </span>
              ) : (
                <>
                  At <strong>{Number.isFinite(effectiveBar) ? effectiveBar.toFixed(2) : "?"}</strong>,{" "}
                  <strong>{preview.clears}</strong> of the last {preview.of} scored items would clear.
                  <span className="bar-track" aria-hidden="true">
                    {recentScores.slice(0, 60).map((s, i) => (
                      <span
                        key={i}
                        className={`bar-tick ${s >= effectiveBar ? "tick-clears" : ""}`}
                        style={{ left: `${Math.min(100, Math.max(0, s * 100))}%` }}
                      />
                    ))}
                    <span
                      className="bar-marker"
                      style={{ left: `${Math.min(100, Math.max(0, (effectiveBar || 0) * 100))}%` }}
                    />
                  </span>
                </>
              )}
            </p>
            {result.errors.min_score && <p className="field-error">{result.errors.min_score}</p>}
          </div>

          <div className={`field ${result.errors.sources ? "field-invalid" : ""}`}>
            <label className="field-label">Sources</label>
            <div className="checkbox-row">
              {KNOWN_SOURCES.map((s) => (
                <label key={s} className="checkbox">
                  <input
                    type="checkbox" checked={form.sources.includes(s)}
                    onChange={(e) => set(
                      "sources",
                      e.target.checked
                        ? [...form.sources, s]
                        : form.sources.filter((x) => x !== s),
                    )}
                  />
                  {s}
                </label>
              ))}
            </div>
            {result.errors.sources && <p className="field-error">{result.errors.sources}</p>}
          </div>

          <div className="field-row">
            <div className={`field ${result.errors.parent_key ? "field-invalid" : ""}`}>
              <label className="field-label" htmlFor="ed-parent">Parent</label>
              <select
                id="ed-parent" value={form.parent_key ?? ""}
                onChange={(e) => set("parent_key", e.target.value || null)}
              >
                <option value="">(none)</option>
                {parentOptions.filter((p) => p.key !== form.key).map((p) => (
                  <option key={p.key} value={p.key}>{p.key}</option>
                ))}
              </select>
              {result.errors.parent_key && <p className="field-error">{result.errors.parent_key}</p>}
            </div>

            <div className="field">
              <label className="field-label" htmlFor="ed-lifecycle">
                Lifecycle<span className="field-hint">active and decaying collect; paused and retired do not</span>
              </label>
              <select
                id="ed-lifecycle" value={form.lifecycle}
                onChange={(e) => set("lifecycle", e.target.value as Lifecycle)}
              >
                <option value="active">active</option>
                <option value="decaying">decaying</option>
                <option value="paused">paused</option>
                <option value="retired">retired</option>
              </select>
            </div>
          </div>

          {result.warnings.map((w) => (
            <p className="editor-warning" key={w}>{w}</p>
          ))}
          {error && <p className="field-error" role="alert">{error}</p>}
        </div>

        <footer className="editor-foot">
          <button type="submit" className="btn btn-primary" disabled={!result.ok || saving}>
            {saving ? "Saving..." : subject.mode === "offer" ? "Accept with these edits" : "Save"}
          </button>
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <span className="prov-muted editor-note">
            {subject.mode === "offer"
              ? "Accepting writes the interest and starts collecting on the next cycle."
              : "Saving rewrites interests.json, re-syncs the database, and takes effect next cycle."}
          </span>
        </footer>
      </form>
    </div>
  );
}
