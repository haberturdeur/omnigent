const CLEAR_PROFILE_NOTIFICATION_ACTIVITY = "omnigent:clear-profile-notification-activity";

export interface ClearProfileNotificationActivityDetail {
  profileId: string;
}

export function clearProfileNotificationActivity(profileId: string): void {
  window.dispatchEvent(
    new CustomEvent<ClearProfileNotificationActivityDetail>(CLEAR_PROFILE_NOTIFICATION_ACTIVITY, {
      detail: { profileId },
    }),
  );
}

export function onClearProfileNotificationActivity(
  listener: (detail: ClearProfileNotificationActivityDetail) => void,
): () => void {
  const handler = (event: Event) => {
    listener((event as CustomEvent<ClearProfileNotificationActivityDetail>).detail);
  };
  window.addEventListener(CLEAR_PROFILE_NOTIFICATION_ACTIVITY, handler);
  return () => window.removeEventListener(CLEAR_PROFILE_NOTIFICATION_ACTIVITY, handler);
}
