import './BrandMark.css'

// The product mark, lifted verbatim out of `Layout.tsx` so the login page --
// which renders outside the app shell -- can show the same one instead of a
// second, drifting copy. Its two fills moved out of `Layout.css` with it.
export default function BrandMark({ size = 22 }: { size?: number }) {
  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 26 26"
      fill="none"
      aria-hidden="true"
    >
      <rect x="2" y="2" width="14" height="22" rx="7" className="brand-mark-soft" />
      <rect x="9" y="2" width="15" height="14" rx="7" className="brand-mark-strong" />
    </svg>
  )
}
