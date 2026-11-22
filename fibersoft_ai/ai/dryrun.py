"""Run an operation inside a savepoint and roll it back, with side-effect guards.

Used so the assistant can test an action (insert / save / submit / cancel / workflow) with the
document's real validations before asking the user to confirm it.
"""

from contextlib import contextmanager

import frappe


class DryRunCommitAttempt(Exception):
	"""Code under test tried to commit or fully roll back; it cannot be safely dry-run."""


def _queue_len(manager):
	funcs = getattr(manager, "_functions", None)
	return len(funcs) if funcs is not None else None


def _truncate(manager, n):
	funcs = getattr(manager, "_functions", None)
	if funcs is None or n is None:
		return
	while len(funcs) > n:
		funcs.pop()


@contextmanager
def dry_run(name="fai_dry_run"):
	db = frappe.db
	orig_commit, orig_rollback = db.commit, db.rollback
	managers = [m for m in (getattr(db, "before_commit", None), getattr(db, "after_commit", None),
							getattr(db, "before_rollback", None), getattr(db, "after_rollback", None)) if m is not None]
	lengths = [_queue_len(m) for m in managers]
	realtime_log = getattr(frappe.local, "_realtime_log", None)
	realtime_len = len(realtime_log) if isinstance(realtime_log, list) else None
	flags_backup = {k: frappe.flags.get(k) for k in ("mute_emails", "mute_messages")}

	def blocked_commit(*args, **kwargs):
		raise DryRunCommitAttempt("commit")

	def guarded_rollback(*args, **kwargs):
		if kwargs.get("save_point") or (args and args[0]):
			return orig_rollback(*args, **kwargs)
		raise DryRunCommitAttempt("rollback")

	db.savepoint(name)
	db.commit, db.rollback = blocked_commit, guarded_rollback
	frappe.flags.mute_emails = True
	frappe.flags.mute_messages = True
	try:
		yield
	finally:
		db.commit, db.rollback = orig_commit, orig_rollback
		try:
			orig_rollback(save_point=name)
		except Exception:
			# savepoint lost (should not happen as commits are blocked) - be safe and roll everything back
			orig_rollback()
		for m, n in zip(managers, lengths):
			_truncate(m, n)
		if realtime_len is not None and isinstance(getattr(frappe.local, "_realtime_log", None), list):
			del frappe.local._realtime_log[realtime_len:]
		for k, v in flags_backup.items():
			frappe.flags[k] = v
		frappe.clear_messages()
