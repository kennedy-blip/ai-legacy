/** Where the source of this build lives. */
export const REPOSITORY_URL = "https://github.com/kennedy-blip/ai-legacy";

const ref = process.env.NEXT_PUBLIC_SOURCE_REF;

/** README section on self-hosting, pinned to the deployed commit when that is known. */
export const RUN_IT_YOURSELF_URL = ref
  ? `${REPOSITORY_URL}/blob/${ref}/README.md#run-it-yourself`
  : `${REPOSITORY_URL}#run-it-yourself`;
