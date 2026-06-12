export interface ParsedTaskResult {
  value?: unknown;
  error?: string;
}

export function extractMarkedJson(text: string): ParsedTaskResult {
  const candidates = fencedJsonBlocks(text);
  for (const candidate of candidates.reverse()) {
    const parsed = tryParse(candidate);
    if (hasMarker(parsed)) return { value: parsed };
  }

  const fallback = tryParse(text.trim());
  if (hasMarker(fallback)) return { value: fallback };

  return {
    error: "No parseable JSON object with QUORUM_TASK_RESULT: true was found.",
  };
}

function fencedJsonBlocks(text: string): string[] {
  const blocks: string[] = [];
  const pattern = /```(?:json)?\s*([\s\S]*?)```/gi;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    blocks.push(match[1].trim());
  }
  return blocks;
}

function tryParse(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

function hasMarker(value: unknown): boolean {
  return (
    !!value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    (value as Record<string, unknown>).QUORUM_TASK_RESULT === true
  );
}
