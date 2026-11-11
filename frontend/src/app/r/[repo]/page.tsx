import type { Metadata } from "next";
import { Suspense } from "react";

import { Centered, Spinner } from "@/ui/common/primitives";
import { Workspace } from "@/ui/repository/Workspace";

export const metadata: Metadata = { title: "Repository" };

// The workspace reads the repository id and the selected tab/node from the URL in the
// browser, so everything below the boundary renders on the client.
export default function Page() {
  return (
    <Suspense
      fallback={
        <Centered>
          <Spinner label="Opening repository" />
        </Centered>
      }
    >
      <Workspace />
    </Suspense>
  );
}
