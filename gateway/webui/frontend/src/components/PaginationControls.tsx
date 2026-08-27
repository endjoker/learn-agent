interface PaginationControlsProps {
  page: number;
  pageSize: number;
  total: number;
  disabled?: boolean;
  onPrevious: () => void;
  onNext: () => void;
}

export function PaginationControls({
  page,
  pageSize,
  total,
  disabled = false,
  onPrevious,
  onNext,
}: PaginationControlsProps) {
  const safePage = Math.max(1, page);
  const start = total === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const end = Math.min(total, safePage * pageSize);
  const hasPrevious = safePage > 1;
  const hasNext = end < total;
  return (
    <nav className="pagination" aria-label="分页">
      <button type="button" className="btn" disabled={disabled || !hasPrevious} onClick={onPrevious}>上一页</button>
      <span>{start}–{end} / {total}</span>
      <button type="button" className="btn" disabled={disabled || !hasNext} onClick={onNext}>下一页</button>
    </nav>
  );
}
