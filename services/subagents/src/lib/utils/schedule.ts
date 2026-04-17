import { CronExpressionParser } from 'cron-parser';

export function calculateNextRun(cronExpression: string, timezone: string = 'UTC'): Date {
  try {
    const interval = CronExpressionParser.parse(cronExpression, { tz: timezone });
    return interval.next().toDate();
  } catch (error) {
    console.error('Error calculating next run time:', error);
    const fallback = new Date();
    fallback.setHours(fallback.getHours() + 1);
    return fallback;
  }
}

export function isValidCron(cronExpression: string): boolean {
  try {
    CronExpressionParser.parse(cronExpression);
    return true;
  } catch {
    return false;
  }
}

export function generateThreadId(): string {
  return `thread_${Date.now()}_${crypto.randomUUID().slice(0, 10)}`;
}
