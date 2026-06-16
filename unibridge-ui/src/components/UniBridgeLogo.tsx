import type { SVGProps } from 'react';

function UniBridgeLogo(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <rect x="4" y="4" width="56" height="56" rx="14" fill="#06111F" />
      <path
        d="M17 36C22.5 26.5 27.5 22 32 22C36.5 22 41.5 26.5 47 36"
        stroke="#50E3C2"
        strokeWidth="5"
        strokeLinecap="round"
      />
      <path
        d="M18 28L32 42L46 28"
        stroke="#0070F3"
        strokeWidth="5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="17" cy="28" r="5.5" fill="#50E3C2" />
      <circle cx="47" cy="28" r="5.5" fill="#0070F3" />
      <circle cx="32" cy="43" r="5.5" fill="#F5A623" />
      <rect x="25" y="25" width="14" height="14" rx="4" fill="#F8FAFC" />
      <path
        d="M29 32H35M32 29V35"
        stroke="#06111F"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <rect x="4.75" y="4.75" width="54.5" height="54.5" rx="13.25" stroke="rgba(80, 227, 194, 0.28)" strokeWidth="1.5" />
    </svg>
  );
}

export default UniBridgeLogo;
