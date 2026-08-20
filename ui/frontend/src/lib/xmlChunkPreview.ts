// A knowledge-base chunk pulled from an XML/BPMN source is indexed and
// retrieved verbatim -- exactly what an agent reads, but unreadable to a
// customer scanning "Try a search" results. XML schemas (BPMN included) put
// the human-authored label of each element in its `name` attribute, so
// pulling just those out gives a plain-language preview; the raw markup
// stays available on demand for anyone who wants to verify it word-for-word.
export interface XmlChunkPreview {
  friendlyText: string
  rawText: string
}

const NAME_ATTR = /\bname="([^"]*)"/g

export function extractXmlChunkPreview(text: string): XmlChunkPreview | null {
  if (!text.trimStart().startsWith('<')) return null
  const names = Array.from(text.matchAll(NAME_ATTR), (m) => m[1].trim()).filter(Boolean)
  if (names.length === 0) return null
  return { friendlyText: names.join('\n'), rawText: text }
}
