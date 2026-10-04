/* ============================================================================
   미리 웹 — 토스쇼핑 상품 카드(인피드) · window.MIRI_SHOP            2026-10-04
   ----------------------------------------------------------------------------
   ★ classic script(모듈 아님). index.html 이 shell.js «앞»에서 로드한다.
     shell.js 의 renderToday / _renderOvChunk / renderRanking / renderTop100 이
     «있으면 쓰고 없으면(구캐시) 종전 그대로» 그린다 — typeof 가드.
   ★ 데이터는 아래 SHOP_DATA 한 덩어리. 손으로 고치지 않는다 — 생성기(build_web_shop.py)가
     쉐어링크 링크 원장과 상품 카탈로그에서 만든다(미니앱 미리와 같은 선정).
     ★이 파일은 sw.js SHELL precache(cache-first)다 → 데이터를 바꾸면 sw.js CACHE 를 «반드시» 범프.
   ★ 배치(광고와 «절대» 붙지 않게 산수로 보장):
       메인 피드 AdFit 인피드 = 카드 7·14·21 번째 «뒤»(MIRI_AD_CONFIG.every=7, 엔진이 꽂음).
       상품 카드              = 카드 3·10·17 번째 «뒤»(n % 7 === 3) → 광고와 앞뒤로 카드 3장 이상 떨어진다.
       ★상품 카드는 class 에 'card' 를 «쓰지 않는다» — 광고 엔진(countCards)이 .card 만 센다.
         .card 를 쓰면 광고 자리가 한 칸씩 밀린다. 모양은 .card 와 같게 .shopcard 가 따로 정의한다.
       랭킹(광고 없음) = 3·13·23 번째 행 «뒤», 순위 카드(.rank) «밖» — 순위 행처럼 읽히지 않게.
       구간 «마지막 카드/행 뒤»에는 두지 않는다(더보기·면책과 붙지 않게) — 큐레이션 구간 끝(다음이 섹션 제목)만 예외.
   ★ 레이아웃 이동 0: 데이터가 번들 안 정적 값이라 카드가 «나중에» 끼어들지 않는다.
     썸네일은 크기 고정 상자 + loading=lazy, 실패하면 아이콘 타일로 바뀐다(크기 동일).
   ★ 링크는 새 탭(target=_blank rel="noopener sponsored"). 자동으로 열지 않는다.
   ⛔ 가격 옆에 날짜 «기준» 문구를 넣지 않는다(모든 앱 공통·영구 규칙).
   ============================================================================ */
(function(){
  'use strict';
  var SHOP_DATA = {"showOfferDetails":true,"disclosure":"[광고] 토스쇼핑 쉐어링크 활동으로, 링크 구매 시 수수료를 지급받습니다.","items":[{"id":"W_484257175","icon":"coffee","label":"개장 전 한 잔","title":"장 시작 전 커피 한 잔","note":"맥심 모카골드 마일드 210개입","cta":"커피 보기","deal":false,"dealUntil":"","until":"2026-12-31","url":"https://toss.im/_m/HQbpy5rs","offer":{"price":30390,"rating":4.7,"reviews":2202,"listPrice":35000,"pct":13,"thumb":"https://shopping.toss.im/live/temp/2026-01-21/339f54d7-ac8d-4d0d-ba5c-b9716cec35b7.png"}},{"id":"W_2625255849","icon":"bag","label":"하루 한 알","title":"바쁜 아침에 챙기는 멀티비타민","note":"일양약품 우리가족 멀티비타민 2통","cta":"비타민 보기","deal":false,"dealUntil":"","until":"2026-12-31","url":"https://toss.shopping/_m/JRB6Zc4A","offer":{"price":4990,"rating":4.7,"reviews":4063,"listPrice":30000,"pct":83,"thumb":"https://shopping.toss.im/live/temp/2026-09-23/710346bd-e4bc-4303-86ad-bc00c2e54660.png"}},{"id":"W_157010575","icon":"bag","label":"장중 뉴스 듣기","title":"노이즈 캔슬링 무선 이어폰","note":"사운독스 노이즈 캔슬링 이어폰 Y-07","cta":"이어폰 보기","deal":false,"dealUntil":"","until":"2026-12-31","url":"https://toss.shopping/_m/zaYtaApn","offer":{"price":19700,"rating":4.6,"reviews":5323,"listPrice":89000,"pct":77,"thumb":"https://shopping.toss.im/live/taca/ai/YjkxZWEw/QVBjR1kvL1J5ZndZR0luMlltSjJIWWZybEJpOTduUm1ZVFZVK1B4VndCY0Q.png"}},{"id":"W_189854565","icon":"coffee","label":"화면 오래 보는 날","title":"구수한 결명자차로 한숨 돌리기","note":"웅진 결명자차 500ml 20병","cta":"차 보기","deal":false,"dealUntil":"","until":"2026-12-31","url":"https://toss.shopping/_m/lH6UCxcw","offer":{"price":13900,"rating":4.8,"reviews":221,"listPrice":21900,"pct":36,"thumb":"https://shopping.toss.im/live/taca/ai/MGI5NDYx/QUprMG56Sk9TenVkTVpoRWJKekduNlZQSExWc01udFNFcTNFck9XRk5IdGM.png"}},{"id":"W_268591409","icon":"bag","label":"오후 장 간식","title":"한 봉씩 꺼내 먹는 초콜릿 아몬드","note":"산과들에 다크초콜릿아몬드 30봉","cta":"간식 보기","deal":false,"dealUntil":"","until":"2026-12-31","url":"https://toss.shopping/_m/97LIGnc7","offer":{"price":19890,"rating":4.8,"reviews":947,"listPrice":21900,"pct":9,"thumb":"https://shopping.toss.im/live/taca/ai/YTI1YWI4/VzZuSlhqYXp5a21Vck4yc3RodXQydHk0YVZ2Ti9NbUpsc2VxeGp5a05rWT0.png"}},{"id":"W_2659903585","icon":"bag","label":"제철 과일","title":"지금이 제철, 서귀포 감귤","note":"제주 서귀포 감귤 5kg","cta":"감귤 보기","deal":false,"dealUntil":"","until":"2026-12-31","url":"https://toss.shopping/_m/LZ6XRWvD","offer":{"price":13900,"rating":4.5,"reviews":785,"listPrice":33000,"pct":57,"thumb":"https://shopping.toss.im/live/temp/2026-09-20/a0e6554a-50c1-4ea9-8954-02f528e674e3.png"}},{"id":"W_2566752469","icon":"coffee","label":"사무실 음료","title":"고소한 곡물차 하루귀리","note":"풀무원 하루귀리 500ml 24병","cta":"곡물차 보기","deal":false,"dealUntil":"","until":"2026-12-31","url":"https://toss.shopping/_m/X1T8lBji","offer":{"price":13990,"rating":4.7,"reviews":393,"listPrice":29000,"pct":51,"thumb":"https://shopping.toss.im/live/temp/2026-09-17/07adc15a-b421-47dd-8995-4f094146441b.jpeg"}},{"id":"W_379824037","icon":"bag","label":"살림 필수품","title":"리뷰 2만 5천 개, 3겹 두루마리 휴지","note":"한예지 3겹 화장지 30롤 2팩","cta":"휴지 보기","deal":false,"dealUntil":"","until":"2026-12-31","url":"https://toss.shopping/_m/DUUK0Xoc","offer":{"price":12800,"rating":4.7,"reviews":25400,"listPrice":37900,"pct":66,"thumb":"https://shopping.toss.im/live/temp/2025-12-02/5e5f2029-7f7c-4511-bbc1-606a56a117b1.jpeg"}}]};
  var URL_RE = /^https:\/\/(toss\.im|toss\.shopping)\/_m\/[A-Za-z0-9]+$/;
  var THUMB_RE = /^https:\/\/(shopping\.toss\.im|static\.toss\.im)\/\S+$/;
  var FEED_EVERY = 7, FEED_OFFSET = 3, FEED_MAX = 3;
  var RANK_EVERY = 10, RANK_OFFSET = 3, RANK_MAX = 3;
  var ICONS = {
    coffee: 'M5 9h11v5a5 5 0 0 1-5 5h-1a5 5 0 0 1-5-5zM16 10.5h1.5a2.5 2.5 0 0 1 0 5H16M8 3.5v2.5M11 3.5v2.5',
    bag: 'M5.5 8h13l-1 12h-11zM9 8V6.5a3 3 0 0 1 6 0V8',
    monitor: 'M4 5h16v10H4zM9 19h6M12 15v4',
    book: 'M5 4.5h9a2 2 0 0 1 2 2V20H7a2 2 0 0 1-2-2V4.5zM5 18a2 2 0 0 1 2-2h9M9 8h4',
    chev: 'M9 5l7 7-7 7'
  };
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]; }); }
  function comma(n){ return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
  function dayKey(d){ return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2); }
  function dayNum(d){ return Math.floor(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86400000); }
  function svg(name, cls){
    return '<svg class="' + cls + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' +
      (name === 'chev' ? '2.4' : '1.8') + '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="' +
      (ICONS[name] || ICONS.bag) + '"/></svg>';
  }
  /* 화면에 낼 수 있는 항목 — 링크 모양·문구·기한 셋 다 통과해야 한다 */
  function live(now){
    if (!SHOP_DATA || !SHOP_DATA.items) return [];
    var today = dayKey(now), out = [];
    for (var i = 0; i < SHOP_DATA.items.length; i++){
      var it = SHOP_DATA.items[i];
      if (!it || !URL_RE.test(it.url || '') || !it.title || !it.label) continue;
      if (it.until && today > it.until) continue;
      out.push(it);
    }
    return out;
  }
  /* 오늘의 순서: 같은 날은 같은 순서(다시 그려도 자리 안 바뀜), 날이 바뀌면 한 칸 민다 */
  function rotation(){
    var now = new Date(), L = live(now), n = L.length, out = [];
    if (!n) return out;
    var s = dayNum(now) % n;
    for (var k = 0; k < n; k++) out.push(L[(s + k) % n]);
    return out;
  }
  function dealOn(it){
    if (!it.deal) return false;
    var u = it.dealUntil || it.until;
    return !u || dayKey(new Date()) <= u;
  }
  function metaLine(o){
    var p = [];
    if (o.rating > 0) p.push('평점 ' + Number(o.rating).toFixed(1));
    if (o.reviews > 0) p.push('리뷰 ' + comma(o.reviews));
    return p.join(' · ');
  }
  function thumbHTML(it, o, cls){
    /* 사진 + 아이콘을 «같은 크기 상자»에 함께 둔다. 사진이 깨지면(아래 error 위임) 상자에 is-icon 을 달아
       아이콘만 보이게 한다 → 레이아웃 이동 0. 인라인 onerror 를 쓰지 않는다(문자열 이스케이프 함정 회피). */
    var ok = !!(o && THUMB_RE.test(o.thumb || ''));
    return '<span class="' + cls + (ok ? '' : ' is-icon') + '">' + svg(it.icon, 'sc-ic') +
      (ok ? '<img src="' + esc(o.thumb) + '" alt="" width="56" height="56" loading="lazy" decoding="async" referrerpolicy="no-referrer">' : '') +
      '</span>';
  }
  function offerOf(it){ return (SHOP_DATA && SHOP_DATA.showOfferDetails) ? it.offer : null; }
  /* 메인 피드 카드 — 공시 카드(.card) 해부 그대로: 머리줄 · 제목 · 본문 상자 · 버튼 · 발 */
  function feedCardHTML(it){
    var o = offerOf(it), price = '', meta = '';
    if (o && o.price > 0){
      price = '<span class="sc-price num">' + (o.pct > 0 ? '<b>' + o.pct + '%</b> ' : '') + comma(o.price) + '원' +
        (o.listPrice > o.price ? ' <s>' + comma(o.listPrice) + '원</s>' : '') + '</span>';
      meta = metaLine(o) ? '<span class="sc-meta">' + esc(metaLine(o)) + '</span>' : '';
    }
    return '<a class="shopcard" href="' + esc(it.url) + '" target="_blank" rel="noopener sponsored" ' +
      'aria-label="' + esc('광고, ' + it.label + ': ' + it.title + '. ' + it.cta + ', 토스쇼핑 새 창') + '" data-shop="' + esc(it.id) + '">' +
      '<span class="sc-top">' + svg(it.icon, 'sc-tic') + '<span class="sc-lb">' + esc(it.label) + '</span><span class="mk">· 토스쇼핑</span>' +
        '<span class="sc-tags">' + (dealOn(it) ? '<span class="sc-pill deal">특가</span>' : '') + '<span class="sc-pill">광고</span></span></span>' +
      '<span class="sc-ttl">' + esc(it.title) + '</span>' +
      '<span class="sc-body">' + thumbHTML(it, o, 'sc-th') +
        '<span class="sc-tx"><span class="sc-nm">' + esc(it.note || '') + '</span>' + price + meta + '</span></span>' +
      '<span class="sc-cta">' + esc(it.cta) + svg('chev', 'sc-chev') + '</span>' +
      '<span class="sc-disc">' + esc(SHOP_DATA.disclosure) + '</span>' +
    '</a>';
  }
  /* 랭킹 카드 — 순위 카드(.rank) 재질 + 순위 행(.rk-row) 리듬, 번호 대신 사진, 값 자리에 「보기」 */
  function rankCardHTML(it){
    var o = offerOf(it), price = '';
    if (o && o.price > 0){
      price = '<span class="sr-price num">' + (o.pct > 0 ? '<b>' + o.pct + '%</b> ' : '') + comma(o.price) + '원' +
        (o.rating > 0 ? '<i>  평점 ' + Number(o.rating).toFixed(1) + '</i>' : '') + '</span>';
    }
    return '<a class="shoprank" href="' + esc(it.url) + '" target="_blank" rel="noopener sponsored" ' +
      'aria-label="' + esc('광고, ' + it.label + ': ' + it.title + '. ' + it.cta + ', 토스쇼핑 새 창') + '" data-shop="' + esc(it.id) + '">' +
      '<span class="sr-row">' + thumbHTML(it, o, 'sr-th') +
        '<span class="sr-info"><span class="sr-nm">' + esc(it.title) + '</span>' + price +
          '<span class="sr-sub"><span class="sc-pill">광고</span>' + (dealOn(it) ? '<span class="sc-pill deal">특가</span>' : '') +
          '<span class="sr-lb">' + esc(it.label) + ' · 토스쇼핑</span></span></span>' +
        '<span class="sr-go">보기' + svg('chev', 'sc-chev') + '</span></span>' +
      '<span class="sc-disc">' + esc(SHOP_DATA.disclosure) + '</span>' +
    '</a>';
  }
  /* 메인 피드: 카드 HTML 배열 → 상품 카드를 끼운 HTML. base = 이 구간 앞에 이미 놓인 공시 카드 수(구간을 넘어 전역 순번).
     자리 k(0,1,2) = 순번 3+7k → 같은 순번이면 늘 같은 상품(재렌더·더보기에도 자리 고정). */
  function weaveFeed(cardsHtml, base, allowTail){
    var rot = rotation(), out = [], n = cardsHtml.length;
    for (var i = 0; i < n; i++){
      out.push(cardsHtml[i]);
      var g = base + i + 1;                      // 전역 순번(1-based)
      /* 구간 마지막 카드 뒤는 금지 — 단 allowTail(큐레이션 구간: 뒤가 「밤사이 공시」 섹션 제목)은 허용.
         밤사이 구간 끝은 「더보기」·면책 문구와 붙으므로 끝까지 금지. */
      if (i === n - 1 && !allowTail) continue;
      if (g % FEED_EVERY !== FEED_OFFSET) continue;
      var k = (g - FEED_OFFSET) / FEED_EVERY;
      if (k < FEED_MAX && k < rot.length) out.push(feedCardHTML(rot[k]));
    }
    return out.join('');
  }
  /* 랭킹: 행 HTML 배열 → 순위 카드를 끊어 그 «사이»에 상품 카드. 상품이 없으면 종전과 «같은» 마크업. */
  function rankHTML(rowsHtml){
    var rot = rotation(), n = rowsHtml.length, parts = [], chunk = [], placed = 0;
    for (var i = 0; i < n; i++){
      chunk.push(rowsHtml[i]);
      var g = i + 1;
      if (i < n - 1 && g % RANK_EVERY === RANK_OFFSET && placed < RANK_MAX && placed < rot.length){
        parts.push('<div class="rank">' + chunk.join('') + '</div>');
        parts.push(rankCardHTML(rot[placed]));
        placed++; chunk = [];
      }
    }
    if (chunk.length) parts.push('<div class="rank">' + chunk.join('') + '</div>');
    return placed ? '<div class="rkstack">' + parts.join('') + '</div>' : parts.join('');
  }
  /* 썸네일 로드 실패 → 아이콘 타일(캡처 단계 위임: img 의 error 는 버블링하지 않는다) */
  document.addEventListener('error', function(e){
    var t = e.target;
    if (!t || t.tagName !== 'IMG' || !t.parentNode || !t.parentNode.classList) return;
    var p = t.parentNode;
    if (p.classList.contains('sc-th') || p.classList.contains('sr-th')) p.classList.add('is-icon');
  }, true);
  window.MIRI_SHOP = { weaveFeed: weaveFeed, rankHTML: rankHTML, _live: function(){ return live(new Date()); } };
})();
