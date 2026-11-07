"use client";

// The settings page (Phase 3 Step 10): who you are, and deleting your
// account. The AI and weekly-email switches, and the CSV export, come with
// PATCH /me/settings in Phase 4 (decided with Dhruv: a switch that can't
// be changed yet would only confuse).

import { DeleteAccount } from "@/components/settings/delete-account";
import { ProfileSection } from "@/components/settings/profile-section";

export function SettingsView() {
  return (
    <div className="flex max-w-xl flex-col gap-8">
      <h1 className="text-2xl font-semibold">Settings</h1>
      <ProfileSection />
      <DeleteAccount />
    </div>
  );
}
