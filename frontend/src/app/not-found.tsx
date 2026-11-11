import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto flex w-full max-w-xl flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
      <p className="eyebrow">404</p>
      <h1 className="text-2xl font-semibold tracking-tight">Nothing at this address</h1>
      <p className="text-sm text-dim">The page may have moved, or the repository was removed.</p>
      <Link href="/" className="btn mt-2">
        Back to repositories
      </Link>
    </main>
  );
}
