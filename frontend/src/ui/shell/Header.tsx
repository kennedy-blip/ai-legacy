import Link from "next/link";

/** Top bar shared by every page. */
export function Header() {
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-ink/85 backdrop-blur">
      <div className="mx-auto flex h-12 w-full max-w-[1600px] items-center gap-4 px-4">
        <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
          <Mark />
          <span>Archaeo-Code</span>
        </Link>
        <span className="hidden text-sm text-faint sm:inline">code archeology for legacy systems</span>
        <nav className="ml-auto flex items-center gap-1 text-sm">
          <Link href="/" className="btn btn-ghost">
            Repositories
          </Link>
          <a href="/api/v1/docs" className="btn btn-ghost" target="_blank" rel="noreferrer">
            API
          </a>
          <a
            href="https://github.com/kennedy-blip/ai-legacy"
            className="btn btn-ghost"
            target="_blank"
            rel="noreferrer"
          >
            GitHub
          </a>
        </nav>
      </div>
    </header>
  );
}

/** Strata: layers of history with one marked find. */
function Mark() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <rect x="1" y="2" width="18" height="4" rx="1.5" fill="#323a4b" />
      <rect x="1" y="8" width="18" height="4" rx="1.5" fill="#4a5468" />
      <rect x="1" y="14" width="18" height="4" rx="1.5" fill="#323a4b" />
      <rect x="11" y="8" width="5" height="4" rx="1" fill="#e5a444" />
    </svg>
  );
}
