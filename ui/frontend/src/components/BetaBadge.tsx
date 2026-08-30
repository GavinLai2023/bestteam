import './BetaBadge.css'

// Sits after the wordmark wherever the product names itself: the nav shell and
// the login page. Not the share page -- its header shows the customer's own
// team name, and a badge there would read as calling that team a beta.
//
// The text is a literal rather than an i18n key on purpose: it is spelled the
// same in both languages, so a key would only give the two copies room to
// drift apart.
export default function BetaBadge() {
  return <span className="beta-badge">beta</span>
}
