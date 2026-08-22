import { describe, it, expect, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import MarkdownText from './MarkdownText'
import { setLanguage } from '../lib/i18n'

describe('MarkdownText', () => {
  afterEach(() => {
    setLanguage('en')
  })

  it('renders a list', () => {
    render(<MarkdownText text={'- one\n- two'} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })

  it('renders a table (gfm)', () => {
    render(<MarkdownText text={'| a | b |\n| - | - |\n| 1 | 2 |'} />)
    expect(screen.getByRole('table')).toBeInTheDocument()
  })

  it('opens links safely', () => {
    render(<MarkdownText text="[docs](https://example.com)" />)
    const link = screen.getByRole('link', { name: 'docs' })
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
    expect(link).toHaveAttribute('rel', expect.stringContaining('nofollow'))
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('renders raw HTML as text, never as markup', () => {
    // Model output is not trusted markup. Without rehype-raw this stays inert.
    render(<MarkdownText text={'<img src=x onerror="alert(1)">'} />)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText(/onerror/)).toBeInTheDocument()
  })

  it('never fetches a remote image a reply asks for', () => {
    // A reply is derived from text the visitor typed, so an image URL is
    // attacker-chosen: rendering an <img> would leak the viewer's IP and
    // access time to that host -- including the admin reading the transcript.
    const { container } = render(<MarkdownText text="![tracker](https://attacker.example/pixel.gif)" />)
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('tracker')).toBeInTheDocument()
  })

  it('labels an image with no alt text', () => {
    render(<MarkdownText text="![](https://attacker.example/pixel.gif)" />)
    expect(screen.getByText('image')).toBeInTheDocument()
  })

  it("labels an alt-less image in the reader's language", () => {
    // Both surfaces that render a reply are bilingual, so the substitute this
    // component puts in place of an <img> has to be translated too (Codex
    // review) -- otherwise a Chinese conversation carries an English word.
    setLanguage('zh-CN')
    render(<MarkdownText text="![](https://attacker.example/pixel.gif)" />)
    expect(screen.getByText('图片')).toBeInTheDocument()
  })

  it('renders plain text unchanged', () => {
    render(<MarkdownText text="Just a sentence." />)
    expect(screen.getByText('Just a sentence.')).toBeInTheDocument()
  })
})
