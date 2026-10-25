// Nested confirmations can outlive the editor they close. Restore body scrolling
// only after the final overlay has gone away, regardless of unmount order.
let locks = 0,
  previous = "";
export function lockOverlay() {
  if (locks++ === 0) {
    previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
}
export function unlockOverlay() {
  if (locks > 0 && --locks === 0) document.body.style.overflow = previous;
}
