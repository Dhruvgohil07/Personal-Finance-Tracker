// A server component, so it can set the page's title (see the accounts
// page); the interactive part is UploadsView.

import { UploadsView } from "@/components/uploads/uploads-view";

export const metadata = { title: "Uploads · Kharcha" };

export default function UploadsPage() {
  return <UploadsView />;
}
