/* Authorized projection; never replace an unavailable count with zero. */
(() => {
  const node = document.getElementById('intake-forgotten');
  if (!node) return;
  async function update() {
    try {
      const response = await fetch('/api/intake/forgotten', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      if (!Number.isInteger(data.forgotten_count)) throw new Error('invalid');
      node.textContent = 'Forgotten: ' + data.forgotten_count;
    } catch { node.textContent = 'Forgotten: unavailable — sign in or retry'; }
  }
  update();
  setInterval(update, 60000);
})();
