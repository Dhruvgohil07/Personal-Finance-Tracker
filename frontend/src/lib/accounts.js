// The user's bank accounts: readable labels and the hooks that read and
// change them through the API (GET/POST/PATCH/DELETE /accounts).
//
// Reading uses TanStack Query's useQuery (fetch + cache); changing uses
// useMutation. After a change the cached list is marked stale
// ("invalidated"), so it is fetched again: the page always shows exactly
// what the backend has.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAuth } from "@/lib/auth";

export const ACCOUNTS_QUERY_KEY = ["accounts"];

// The backend's BankCode and AccountType values (app/models/account.py),
// with the words the user sees.
export const BANK_OPTIONS = [
  { value: "ICICI", label: "ICICI Bank" },
  { value: "HDFC", label: "HDFC Bank" },
  { value: "SBI", label: "State Bank of India" },
  { value: "GENERIC", label: "Another bank" },
];

export const ACCOUNT_TYPE_OPTIONS = [
  { value: "savings", label: "Savings" },
  { value: "current", label: "Current" },
  { value: "credit_card", label: "Credit card" },
];

function labelFor(options, value) {
  return options.find((option) => option.value === value)?.label ?? value;
}

export const bankLabel = (code) => labelFor(BANK_OPTIONS, code);
export const accountTypeLabel = (type) => labelFor(ACCOUNT_TYPE_OPTIONS, type);

// Exactly 4 digits: the same rule as the backend's MaskedNumber.
export const LAST_FOUR_PATTERN = /^[0-9]{4}$/;

/** `{ data: [account, …], isPending, error }`; only asks while signed in. */
export function useAccounts() {
  const { status, client } = useAuth();
  return useQuery({
    queryKey: ACCOUNTS_QUERY_KEY,
    queryFn: ({ signal }) => client.request("/accounts", { signal }),
    enabled: status === "authenticated",
  });
}

/** Add an account: `mutateAsync({ bank_code, nickname, account_type, masked_number })`. */
export function useCreateAccount() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (account) => client.request("/accounts", { method: "POST", body: account }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ACCOUNTS_QUERY_KEY }),
  });
}

/** Rename or retype: `mutateAsync({ id, changes: { nickname?, account_type? } })`. */
export function useUpdateAccount() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, changes }) =>
      client.request(`/accounts/${id}`, { method: "PATCH", body: changes }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ACCOUNTS_QUERY_KEY }),
  });
}

/** Delete an account and everything imported into it: `mutateAsync(id)`. */
export function useDeleteAccount() {
  const { client } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id) => client.request(`/accounts/${id}`, { method: "DELETE" }),
    // ALL cached data, not just the list: the account's uploads and
    // transactions are gone too, so any page showing them must refetch.
    onSuccess: () => queryClient.invalidateQueries(),
  });
}
