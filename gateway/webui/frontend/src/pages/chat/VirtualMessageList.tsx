import { useVirtualizer } from "@tanstack/react-virtual";
import type { ReactNode } from "react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

export interface VirtualItem { key: string }

const NEAR_BOTTOM_PX = 96;
// 首屏定位后的高度稳定窗口：基础 1.2s，每次因高度变化重钉底顺延 0.4s，
// 距首屏定位最长 5s（图片等慢资源超出后交给常规贴底跟随/用户手动跳转）。
const SETTLE_BASE_MS = 1200;
const SETTLE_EXTEND_MS = 400;
const SETTLE_MAX_MS = 5000;

export function VirtualMessageList<T extends VirtualItem>({
  items,
  renderItem,
  estimateSize = () => 44,
  onNearTop,
  className = "msg-area virtual-msg-area",
  autoFollow = false,
}: {
  items: T[];
  renderItem: (item: T, index: number) => ReactNode;
  estimateSize?: (index: number) => number;
  onNearTop?: () => void;
  className?: string;
  /** 会话运行中强制跟随最新输出（旧版聊天页行为）。 */
  autoFollow?: boolean;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const initialized = useRef(false);
  const stickToBottom = useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);
  // ---- 首屏定位（修复"打开会话从顶部滚到底部"的闪烁）----
  // initialReady=false 时隐藏列表内容：历史消息首次挂载时容器高度未定、
  // Markdown/图片/代码块异步改变高度，若直接显示，用户会先看到 scrollTop=0
  // 的顶部画面，再被滚到底部。定位完成（布局效应内同步滚到底）后才揭示。
  // 揭示时机（关键）：不能与"估算尺寸滚底"同帧揭示——估算高度（44-76px）
  // 远小于 Markdown 气泡真实高度，估算布局的"底部"在真实布局里对应的是
  // 偏顶部的历史区域；必须等首轮真实测量（remeasureAll）写回并重新钉底后
  // 再揭示，否则用户会先看到历史顶部、再跳到输入框位置。安全兜底：init 时
  // 起 SETTLE_MAX_MS 定时器，即使重测回调未运行也会强制揭示（退化为旧行为，
  // 不会卡在隐藏态）。
  const [initialReady, setInitialReady] = useState(false);
  const pendingRevealRef = useRef(false);
  const revealTimerRef = useRef(0);
  // 定位后的"高度稳定窗口"：Markdown/图片渲染、虚拟测量校准会持续改变
  // 总高度，窗口内贴底时反复钉在底部（无动画），窗口外交给常规跟随逻辑。
  const settleDeadlineRef = useRef(0);
  const settleInitAtRef = useRef(0);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize,
    getItemKey: (index) => items[index]?.key ?? index,
    overscan: 8,
  });
  const virtualItems = virtualizer.getVirtualItems();

  // ---- 前插历史保持滚动位置 ----
  // 锚点：最近一次滚动时第一个可见项的 key 与其相对视口顶部的偏移；
  // 前插发生时按"新内容偏移 - 原视口偏移"恢复 scrollTop。
  const anchorRef = useRef<{ key: string; viewportOffset: number } | null>(null);
  const prependRestoreRef = useRef<{ key: string; viewportOffset: number } | null>(null);
  // onNearTop 锁存：加载完成（items 变化）或滚离顶部前不连发
  const nearTopLockRef = useRef(false);

  const scrollToBottom = useCallback(() => {
    const el = parentRef.current;
    if (!el) return;
    // rAF：让布局/测量先完成再滚动，流式更新时更稳
    requestAnimationFrame(() => {
      if (!parentRef.current) return;
      virtualizer.scrollToIndex(items.length - 1, { align: "end" });
      const node = parentRef.current;
      if (node && node.scrollTop + node.clientHeight < node.scrollHeight - 2) {
        node.scrollTop = node.scrollHeight;
      }
    });
  }, [items.length, virtualizer]);

  const jumpToLatest = useCallback(() => {
    stickToBottom.current = true;
    setShowJumpToLatest(false);
    setUnreadCount(0);
    scrollToBottom();
  }, [scrollToBottom]);

  // ---- 首次定位与新消息自动滚动严格分离（设计方案：首屏不闪烁）----
  // items 从空变非空（历史首页到达）→ useLayoutEffect 在浏览器绘制前同步
  // 定位到底部（无动画 scrollTop=scrollHeight，估算高度下"底部"仍是底部），
  // 同一帧内揭示列表——用户看不到顶部画面。切换会话（items 清空）时复位，
  // 下一个会话重新走"隐藏 → 定位 → 揭示"。
  useLayoutEffect(() => {
    if (items.length === 0) {
      if (initialized.current) {
        initialized.current = false;
        setInitialReady(false);
        pendingRevealRef.current = false;
        if (revealTimerRef.current) {
          window.clearTimeout(revealTimerRef.current);
          revealTimerRef.current = 0;
        }
        // 会话切换：清空上一会话的测量缓存（按 item key 存活于 virtualizer
        // 实例，跨会话不失效），杜绝任何陈旧尺寸泄漏进下一个会话。
        virtualizer.measure();
      }
      return;
    }
    if (initialized.current) return;
    initialized.current = true;
    const el = parentRef.current;
    if (el) {
      virtualizer.scrollToIndex(items.length - 1, { align: "end" });
      el.scrollTop = el.scrollHeight;
    }
    settleInitAtRef.current = performance.now();
    settleDeadlineRef.current = settleInitAtRef.current + SETTLE_BASE_MS;
    // 揭示推迟：等 remeasureAll 首轮真实测量写回并重新钉底后再揭示
    pendingRevealRef.current = true;
    if (revealTimerRef.current) window.clearTimeout(revealTimerRef.current);
    revealTimerRef.current = window.setTimeout(() => {
      if (pendingRevealRef.current) {
        pendingRevealRef.current = false;
        setInitialReady(true);
      }
    }, SETTLE_MAX_MS);
  }, [items.length, virtualizer]);

  // 重读所有已挂载 wrapper 的真实高度，写回 virtualizer（rAF 节流合并）。
  // 关键实现约束：不能用 virtualizer.measureElement(node) 做「增量重测」——
  // virtual-core 的 measureElement 在手动调用（entry=null）时是 cache-first：
  // itemSizeCache 命中即直接返回旧尺寸，永远拿不到真实高度。若依赖它，
  // 「open 属性变化 / 内容变化 → 重测」的兜底是空转：展开卡片后 virtualizer
  // 仍按折叠旧高度布局，后续行叠压；陈旧条目按 item key 存活于 virtualizer
  // 实例，切换会话（key 全换）或刷新（重挂载）才复位。
  // 正确姿势：直接读 wrapper 的真实 offsetHeight，经公开 API resizeItem
  // 写回（尺寸未变时 delta=0 开销可忽略；与内部 ResizeObserver 写同一
  // 真实值，幂等无冲突）。
  const remeasureRafRef = useRef(0);
  const remeasureAll = useCallback(() => {
    cancelAnimationFrame(remeasureRafRef.current);
    remeasureRafRef.current = requestAnimationFrame(() => {
      parentRef.current?.querySelectorAll<HTMLElement>("[data-virtual-index]").forEach((node) => {
        const index = Number(node.getAttribute("data-index"));
        if (!Number.isInteger(index) || index < 0) return;
        const measured = node.offsetHeight;
        if (measured <= 0) return; // 未布局/隐藏（首屏揭示前）不写回
        virtualizer.resizeItem(index, measured);
      });
      // 首屏稳定窗口内：测量校准改变了总高度（Markdown/图片/代码块渲染），
      // 贴底时重新钉底（无动画），避免"定位到底部后又被高度变化顶离"。
      // 用户上翻（stickToBottom=false）立即停手，不强制拉回。
      const el = parentRef.current;
      if (el && initialized.current && stickToBottom.current) {
        const now = performance.now();
        if (now < settleDeadlineRef.current) {
          el.scrollTop = el.scrollHeight;
          settleDeadlineRef.current = Math.min(
            settleInitAtRef.current + SETTLE_MAX_MS,
            now + SETTLE_EXTEND_MS,
          );
        }
      }
      // 首屏揭示：首轮真实测量已写回并重新钉底，此刻揭示的第一帧即真底部
      if (pendingRevealRef.current) {
        pendingRevealRef.current = false;
        setInitialReady(true);
      }
    });
  }, [virtualizer]);

  // items 引用变化（live→history 切换、流式追加、前插历史）→ 重新测量；
  // 同时检测前插：记录锚点，测量完成后恢复滚动位置。
  const prevItemsRef = useRef<T[] | null>(null);
  useLayoutEffect(() => {
    const prev = prevItemsRef.current;
    prevItemsRef.current = items;
    if (prev !== items) {
      const prevFirst = prev?.[0]?.key ?? null;
      const nextFirst = items[0]?.key ?? null;
      // 前插判定：首项 key 变化、总数增加，且此前有可见锚点
      if (prevFirst !== null && nextFirst !== null && prevFirst !== nextFirst
        && (prev?.length ?? 0) > 0 && items.length > (prev?.length ?? 0) && anchorRef.current) {
        prependRestoreRef.current = { ...anchorRef.current };
      }
      // 数据到达（加载完成）→ 解除 onNearTop 锁存
      nearTopLockRef.current = false;
      requestAnimationFrame(remeasureAll);
    }
  }, [items, remeasureAll]);

  // 前插后恢复滚动位置：等测量完成后按锚点的新内容偏移恢复（原视口偏移不变）。
  useLayoutEffect(() => {
    const anchor = prependRestoreRef.current;
    if (!anchor) return;
    prependRestoreRef.current = null;
    const el = parentRef.current;
    if (!el) return;
    const newIndex = items.findIndex((item) => item.key === anchor.key);
    if (newIndex < 0) return;
    // 双 rAF：确保 remeasureAll 的测量已应用后再取偏移
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const node = parentRef.current;
        if (!node) return;
        const position = virtualizer.getOffsetForIndex(newIndex);
        if (position == null) return;
        node.scrollTop = Math.max(0, position[0] - anchor.viewportOffset);
      });
    });
  }, [items, virtualizer]);

  // 兜底测量：measureElement 的 ResizeObserver 在 flex 滚动容器 + 绝对定位
  // wrapper 下可能不触发，导致流式增长的条目（如运行进度卡）在 virtualizer 缓存里
  // 仍是 estimateSize，后续条目重叠。
  // 1) MutationObserver：观察内容/字符变化（流式文本增长）+ open 属性变化
  //    （details 卡片展开/收起是属性变更，不监听会导致展开后正文溢出条目、
  //    被后续卡片叠压——virtualizer 缓存高度仍是折叠时的估值）；
  // 2) ResizeObserver：直接观察每个 wrapper 的尺寸变化（增量 observe/unobserve）。
  useEffect(() => {
    const el = parentRef.current;
    if (!el) return undefined;
    let raf = 0;
    const schedule = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => remeasureAll());
    };
    const mo = new MutationObserver(schedule);
    mo.observe(el, { childList: true, subtree: true, characterData: true,
                     attributes: true, attributeFilter: ["open"] });
    const ro = new ResizeObserver(schedule);
    const observed = new Set<Element>();
    const syncObserve = () => {
      const nodes = el.querySelectorAll<HTMLElement>("[data-virtual-index]");
      for (const node of nodes) {
        if (!observed.has(node)) {
          observed.add(node);
          ro.observe(node);
        }
      }
      // 清理已卸载节点的观察（unobserve），避免残留引用/重复回调
      for (const node of observed) {
        if (!node.isConnected) {
          ro.unobserve(node);
          observed.delete(node);
        }
      }
    };
    syncObserve();
    const roMo = new MutationObserver(syncObserve);
    roMo.observe(el, { childList: true, subtree: true });
    return () => {
      cancelAnimationFrame(raf);
      cancelAnimationFrame(remeasureRafRef.current);
      mo.disconnect();
      roMo.disconnect();
      for (const node of observed) ro.unobserve(node);
      observed.clear();
      ro.disconnect();
    };
  }, [remeasureAll]);

  // 仅在列表首次初始化或用户仍贴底且列表项数量变化时跟随。
  // 绝不能在每次渲染/每个 token 到达时调用 scrollToBottom，否则用户上滑会被拉回底部。
  const previousLengthRef = useRef(items.length);
  useLayoutEffect(() => {
    const lengthChanged = previousLengthRef.current !== items.length;
    previousLengthRef.current = items.length;
    if (!initialized.current || !lengthChanged || !stickToBottom.current) return;
    scrollToBottom();
  }, [items.length, scrollToBottom]);

  // 空闲时：只有用户本来就在底部附近才跟随新消息；否则累计未读数
  useEffect(() => {
    if (!initialized.current || autoFollow) return;
    if (stickToBottom.current && items.length > 0) {
      scrollToBottom();
    } else if (items.length > 0) {
      setUnreadCount((prev) => prev + 1);
    }
  }, [items.length, autoFollow, scrollToBottom]);

  // 流式滚动跟随（收官优化）：busy（autoFollow）且用户贴底时，对 live 文本
  // 总长做 rAF 节流的 scrollTop=scrollHeight。items.length 不变（同一条消息/
  // 卡片内文本增长）时，上面的 length 跟随不会触发，这里兜底——只在 live
  // 文本确实变长时调度，每帧至多一次；用户上滑（stickToBottom=false）即停。
  const liveTextLength = useMemo(() => {
    let total = 0;
    for (const raw of items) {
      const item = raw as unknown as {
        kind?: string; live?: boolean; text?: string; liveText?: string;
        message?: { kind?: string; content_text?: string };
      };
      if (item.kind === "reasoning") {
        if (item.live) total += item.text?.length ?? 0;
      } else if (item.kind === "projection") {
        total += item.liveText?.length ?? 0;
      } else if (item.kind === "message") {
        if (item.message?.kind !== "final") total += item.message?.content_text?.length ?? 0;
      }
    }
    return total;
  }, [items]);
  const lastLiveLengthRef = useRef(-1);
  const liveScrollRafRef = useRef(0);
  useEffect(() => {
    if (!autoFollow || !stickToBottom.current || !initialized.current) return;
    if (liveTextLength === lastLiveLengthRef.current) return;
    lastLiveLengthRef.current = liveTextLength;
    cancelAnimationFrame(liveScrollRafRef.current);
    liveScrollRafRef.current = requestAnimationFrame(() => {
      const el = parentRef.current;
      if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(liveScrollRafRef.current);
  }, [autoFollow, liveTextLength]);

  return (
    <div className="virtual-message-list-wrap">
      <div
        ref={parentRef}
        className={className}
        // 首屏定位完成前隐藏内容（保留布局尺寸）：避免用户看到"顶部 → 滚到底部"
        // 的初始化过程；页面自身的"加载历史消息…"提示仍可见。
        style={items.length > 0 && !initialReady ? { visibility: "hidden" } : undefined}
        onScroll={(event) => {
        const el = event.currentTarget;
        const nearBottom = el.scrollTop + el.clientHeight
          >= el.scrollHeight - NEAR_BOTTOM_PX;
        stickToBottom.current = nearBottom;
        setShowJumpToLatest(!nearBottom && items.length > 0);
        if (nearBottom) setUnreadCount(0);
        // 记录滚动锚点（第一个可见虚拟项 + 相对视口顶部的偏移）
        const first = virtualItems[0];
        if (first) {
          anchorRef.current = { key: String(first.key), viewportOffset: first.start - el.scrollTop };
        }
        if (initialized.current && el.scrollTop < 160) {
          // 锁存：加载完成（items 变化）或滚离顶部前不连发
          if (!nearTopLockRef.current) {
            nearTopLockRef.current = true;
            onNearTop?.();
          }
        } else {
          nearTopLockRef.current = false;
        }
      }}>
        <div style={{ height: `${virtualizer.getTotalSize()}px`, width: "100%", position: "relative" }}>
          {virtualItems.map((virtualRow) => {
            const item = items[virtualRow.index];
            if (!item) return null;
            return (
              <div
                key={item.key}
                data-index={virtualRow.index}
                data-virtual-index={virtualRow.index}
                ref={virtualizer.measureElement}
                style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${virtualRow.start}px)` }}
              >
                {renderItem(item, virtualRow.index)}
              </div>
            );
          })}
        </div>
      </div>
      {showJumpToLatest ? (
        <button
          type="button"
          className="jump-to-latest"
          title="跳转到最新回复"
          aria-label="跳转到最新回复"
          onClick={jumpToLatest}
        >
          {unreadCount > 0 ? `⌄ ${unreadCount}` : "⌄"}
        </button>
      ) : null}
    </div>
  );
}
