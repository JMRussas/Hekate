//  Hekate VSCode Extension - Secret Redaction
//
//  Scrubs known secret patterns from text before it enters
//  conversation history or gets forwarded to LLM providers.
//
//  Depends on: (none)
//  Used by:    views/chatView.ts

/** Patterns that match known secret formats. Each has a label for the replacement. */
const SECRET_PATTERNS: Array<{ re: RegExp; label: string }> = [
  // Orchestration API keys: orch_ + 64 hex chars
  { re: /orch_[0-9a-fA-F]{16,}/g, label: "[REDACTED:api-key]" },
  // JWT tokens (three base64url segments separated by dots)
  { re: /eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}/g, label: "[REDACTED:jwt]" },
  // Bearer tokens in plain text
  { re: /Bearer\s+[A-Za-z0-9_.-]{20,}/gi, label: "Bearer [REDACTED:token]" },
  // Generic hex secrets (64+ hex chars, likely keys/hashes)
  { re: /(?<![0-9a-fA-F])[0-9a-fA-F]{64,}(?![0-9a-fA-F])/g, label: "[REDACTED:hex-secret]" },
];

/**
 * Replace known secret patterns with redaction placeholders.
 * Safe to call on any string — returns the original if nothing matches.
 */
export function redactSecrets(text: string): string {
  let result = text;
  for (const { re, label } of SECRET_PATTERNS) {
    // Reset regex lastIndex for global patterns
    re.lastIndex = 0;
    result = result.replace(re, label);
  }
  return result;
}
