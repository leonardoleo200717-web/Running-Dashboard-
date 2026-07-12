// Origin detection: structured workout iff a workout message exists.
export const STRUCTURED = 'structured';
export const FREE = 'free';

export function detectOrigin(activity) {
  const isStructured = activity.workout !== null ||
    (activity.workout_steps && activity.workout_steps.length > 0);
  return {
    origin: isStructured ? STRUCTURED : FREE,
    manufacturer: (activity.file_id || {}).manufacturer ?? null,
    wkt_name: activity.workout ? activity.workout.wkt_name : null,
  };
}
