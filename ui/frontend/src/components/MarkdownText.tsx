import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './MarkdownText.css'

// One renderer for a team's reply, shared by the anonymous visitor page
// (pages/ShareChatPage.tsx) and the org-side audit transcript
// (components/SharedSessionsPanel.tsx) -- an admin reviewing a conversation
// must see exactly what the visitor saw.
//
// No `rehype-raw`, on purpose: model output is not trusted markup, and without
// it raw HTML stays inert text. That property is the reason this is a library
// rather than a hand-rolled renderer.
//
// A visitor's own message is NOT rendered through this: their typing is not
// markup, and `white-space: pre-wrap` already preserves its line breaks.
export default function MarkdownText({ text }: { text: string }) {
  return (
    <div className="markdown-text">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => <a {...props} target="_blank" rel="noopener noreferrer nofollow" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
