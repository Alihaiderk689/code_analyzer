// Hexagon-outlined "</>" brand mark. Uses currentColor so it inherits the
// same theme-adaptive color the old CSS-only mark had (var(--color-text)).
export default function LogoMark({ size = 28, className }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 100 100"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      style={{ color: 'var(--color-text)', flexShrink: 0 }}
      aria-hidden="true"
    >
      <path
        d="M50 8 L86.4 29 L86.4 71 L50 92 L13.6 71 L13.6 29 Z"
        stroke="currentColor"
        strokeWidth="8"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <path d="M43 36 L29 50 L43 64" stroke="currentColor" strokeWidth="8" strokeLinejoin="round" strokeLinecap="round" />
      <path d="M57 36 L71 50 L57 64" stroke="currentColor" strokeWidth="8" strokeLinejoin="round" strokeLinecap="round" />
      <path d="M58 30 L42 70" stroke="currentColor" strokeWidth="8" strokeLinecap="round" />
    </svg>
  )
}
