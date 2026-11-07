// A server component, so it can set the page's title (see the accounts
// page); the interactive part is SettingsView.

import { SettingsView } from "@/components/settings/settings-view";

export const metadata = { title: "Settings · Kharcha" };

export default function SettingsPage() {
  return <SettingsView />;
}
