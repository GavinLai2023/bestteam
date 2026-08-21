import { describe, expect, it } from 'vitest'
import { extractXmlChunkPreview } from './xmlChunkPreview'

describe('extractXmlChunkPreview', () => {
  it('returns null for a plain-language chunk (not XML)', () => {
    expect(extractXmlChunkPreview('Returns are accepted within 30 days.')).toBeNull()
  })

  it('returns null for XML with no name attributes to pull out', () => {
    expect(extractXmlChunkPreview('<bpmn:incoming>Flow_1warbme</bpmn:incoming>')).toBeNull()
  })

  it('joins every name="..." attribute value, in order', () => {
    const chunk =
      '<bpmn:userTask id="Activity_0q2b8im" name="The employee is based at Capalaba Library">\n' +
      '  <bpmn:userTask id="Activity_1gm0ga0" name="Hours are 6am to 6pm">'
    const preview = extractXmlChunkPreview(chunk)
    expect(preview?.friendlyText).toBe('The employee is based at Capalaba Library\nHours are 6am to 6pm')
    expect(preview?.rawText).toBe(chunk)
  })

  it('ignores an empty name attribute', () => {
    const chunk = '<bpmn:process id="P1" name="">\n<bpmn:userTask name="Real label">'
    expect(extractXmlChunkPreview(chunk)?.friendlyText).toBe('Real label')
  })
})
