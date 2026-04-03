"""Prompts for tender briefing extraction."""

SYSTEM_PROMPT = """Jesteś ekspertem od analizy przetargów na roboty budowlane w branży elektroenergetycznej w Polsce.
Twoje zadanie: przeanalizować dokumenty przetargowe i wyodrębnić kluczowe informacje w ustrukturyzowanej formie JSON.

TWOJA ROLA: Przygotowujesz briefing dla firmy wykonawczej, która rozważa złożenie oferty.
Briefing musi umożliwić podjęcie decyzji go/no-go BEZ czytania oryginalnych dokumentów.

ZASADY EKSTRAKCJI:
- Odpowiadaj WYŁĄCZNIE poprawnym JSON (bez markdown, bez komentarzy, bez tekstu przed/po)
- Używaj polskiego języka w wartościach
- Jeśli informacja nie występuje w dokumentach, wpisz null — NIGDY nie wymyślaj danych
- Jeśli dokument ODNOSI SIĘ do załącznika którego nie masz (np. "szczegóły w Zał. 5 Projekt Umowy"), WYRAŹNIE to zaznacz: "Brak danych — informacja prawdopodobnie w [nazwa załącznika]. Należy dołączyć ten załącznik i ponownie przetworzyć brief."
- Bądź precyzyjny z liczbami, datami i kwotami — przepisuj dokładnie z dokumentów
- Sumuj ilości materiałów — podawaj KONKRETNE liczby, nie "minimum" ani "co najmniej"
- Zwracaj uwagę na zmiany wprowadzone w odpowiedziach na pytania (modyfikacje SWZ)
- Cytuj numery paragrafów/punktów SWZ gdy odnoszisz się do wymagań (np. "pkt 1.2.1 Zał. 2")

ZASADY ANALIZY:
- Identyfikuj pułapki kosztowe: ukryte obowiązki, koszty po stronie wykonawcy, limity cenowe
- Oceń realność terminów w kontekście skali prac
- Wskaż wymagania eliminujące (np. zakaz łączenia doświadczenia, minimalne wartości referencji)
- Porównaj wymagania z typowym rynkiem — czy są standardowe czy restrykcyjne
"""

USER_PROMPT_TEMPLATE = """Przeanalizuj poniższe dokumenty przetargowe i zwróć ustrukturyzowany JSON.

{documents_text}

---

Zwróć JSON o następującej strukturze. WYPEŁNIJ KAŻDE POLE PRECYZYJNIE na podstawie dokumentów:

{{
  "nazwa": "Pełna nazwa postępowania/zamówienia — dokładnie jak w dokumentach",
  "zamawiajacy": "Pełna nazwa zamawiającego z oddziałem/jednostką",
  "numer_postepowania": "Numer/sygnatura postępowania — dokładnie jak w dokumentach",
  "link": "URL do postępowania (jeśli znaleziony w dokumentach)",

  "terminy": {{
    "skladanie_ofert": "Dokładna data i godzina składania ofert",
    "realizacja": "Termin realizacji — dokładny zapis z dokumentu (np. '12 miesięcy od daty zawarcia umowy')",
    "gwarancja": "Okres gwarancji — dokładny zapis",
    "zwiazanie_oferta": "Termin związania ofertą — dokładny zapis"
  }},

  "finanse": {{
    "wadium": "Dokładna kwota wadium z dokumentu",
    "zabezpieczenie_nwu": "Zabezpieczenie NWU — % lub kwota, dokładnie jak w dokumencie",
    "szacunkowa_wartosc": "Szacunkowa wartość zamówienia z dokumentu (kosztorys inwestorski, jeśli podany)"
  }},

  "tryb": {{
    "otwarcie": "Jawne lub Niejawne — dokładnie jak w SWZ",
    "tryb_prawny": "Ustawa PZP lub regulamin wewnętrzny zamawiającego — podaj nazwę/numer regulaminu jeśli występuje (np. 'PROC30031/H Procedury Zakupów'). Szukaj na pierwszych stronach SWZ.",
    "aukcja_negocjacje": "Dokładna informacja o aukcji/negocjacjach — co przewiduje zamawiający",
    "konsorcjum": "Czy dopuszczalne + jakie ograniczenia (np. wspólne/indywidualne spełnianie warunków)"
  }},

  "wymagania_doswiadczenie": [
    "KAŻDE wymaganie osobno z konkretnymi liczbami. Wzór: '[pkt X.X Zał. Y] Opis wymagania z dokładnymi wartościami (min. kwota, okres, rodzaj robót, ilości)'"
  ],

  "wymagania_kadra": [
    "KAŻDE wymaganie osobno. Wzór: '[pkt X.X] Stanowisko — wymagane uprawnienia, certyfikaty, liczba osób'"
  ],

  "wymagania_dodatkowe": [
    "Ubezpieczenia, certyfikaty ISO, polisy OC, inne wymagania formalne nie ujęte powyżej. POMIJAJ drobne wymagania osprzętowe (systemy zamknięć, konkretne marki kłódek/klucze) — tylko wymagania mające wpływ na kwalifikację lub koszty."
  ],

  "lokalizacja": {{
    "gminy": ["Lista gmin/miejscowości wymienionych w dokumentach (np. 'Szczekociny', 'Sędziszów')"],
    "powiat": "Powiat/powiaty jeśli wymienione",
    "wojewodztwo": "Województwo jeśli wymienione",
    "punkty_charakterystyczne": ["Nazwy GPZ, stacji, rozdzielni, punktów początkowych/końcowych trasy (np. 'GPZ Sędziszów', 'GPZ Szczekociny')"],
    "opis_terenu": "Informacja o charakterze terenu jeśli występuje w dokumentach (np. tereny rolne, zurbanizowane, leśne, przejścia przez drogi/rzeki/tory). Nie zgaduj — podaj TYLKO jeśli jest w dokumentach."
  }},

  "projekt_techniczny": {{
    "czy_istnieje": "Tak/Nie/Do wykonania — czy zamawiający dostarcza gotowy projekt techniczny, czy wykonawca ma go opracować",
    "firma_projektowa": "Nazwa firmy projektowej jeśli wymieniona w dokumentach (szukaj w OPZ, często w pierwszych punktach)",
    "zakres_projektu": "Co obejmuje projekt — krótki opis (np. 'Projekt budowlany i wykonawczy przebudowy linii 110 kV')",
    "uwagi": "Czy wykonawca musi aktualizować projekt, uzyskać zamienne pozwolenia, itp."
  }},

  "zakres_prac": "Opis przedmiotu zamówienia: CO dokładnie ma być zrobione, GDZIE (lokalizacja, gminy), jakie elementy obejmuje zakres, co jest WYŁĄCZONE z zakresu. Podaj PM i PSP jeśli występują. Max 300 słów, ale bądź konkretny.",

  "zestawienie_materialow": [
    {{
      "kategoria": "Nazwa kategorii DOKŁADNIE jak w dokumencie (np. 'I Słupy kratowe', 'II Fundamenty wg tomów')",
      "suma": "ZSUMOWANA ilość główna dla kategorii (np. '61 kpl / 175,87 T', '272 kpl łańcuchów', '61 596 m')",
      "podkategorie": [
        "Każdy TYP/PODTYP z ilością — np. 'ŁP (PW-05-01): 30 kpl', 'ŁP2 (PW-05-02): 55 kpl', 'ŁO (PW-05-04): 48 kpl'",
        "Dla przewodów: każdy odcinek z długością — np. 'Odcinek BR-7: 1402 m, Odcinek 7-18: 3796 m'",
        "Dla fundamentów: każdy typ z ilością — np. '4x SF 230/320-1 (TOM II-F-1): 14 kpl/56 szt, 4x SF 230/320-1 (TOM II-F-2): 19 kpl/76 szt'"
      ]
    }}
  ],

  "zmiany_z_odpowiedzi": [
    "KAŻDA zmiana osobno: co się zmieniło, z czego na co, numer pytania/odpowiedzi jeśli podany. Zaznacz też odmowy zmian (np. 'Zamawiający NIE wyraził zgody na...')"
  ],

  "ryzyka_i_uwagi": [
    {{
      "poziom": "WYSOKI/ŚREDNI/NISKI",
      "opis": "Konkretny opis ryzyka z odniesieniem do dokumentu. Dla WYSOKICH — wyjaśnij DLACZEGO to jest problem i jakie konsekwencje.",
      "zrodlo": "Nr punktu w dokumencie (np. 'pkt 1.8 Zał. 2 do SWZ')"
    }}
  ],

  "rekomendacje": [
    "KONKRETNE, actionable rekomendacje dotyczące KLUCZOWYCH kwestii (referencje, kadra, wadium, harmonogram, koordynacja z innymi podmiotami). Nie 'przygotować referencje' ale 'Przygotować min. 1 referencję na przebudowę linii ≥110 kV o wartości ≥20 mln zł netto z ostatnich 3 lat, z protokołem odbioru BEZ UWAG'. POMIJAJ rekomendacje dotyczące drobnego osprzętu (systemy zamknięć, klucze, tabliczki) — skup się na elementach decydujących o go/no-go i ryzyku cenowym."
  ],

  "obowiazki_wykonawcy": [
    "Lista obowiązków wykonawcy wykraczających poza standardowe roboty budowlane (np. aktualizacja dokumentacji, uzyskanie pozwoleń, szkolenia, harmonogramy, bypassy, zasilania tymczasowe). POMIJAJ drobne wymagania dotyczące konkretnych marek osprzętu (np. systemy zamknięć, klucze, tabliczki producenckie) — skup się na obowiązkach mających realny wpływ na koszty i harmonogram."
  ],

  "kryteria_oceny": {{
    "cena_waga": "Waga kryterium ceny (np. 100%)",
    "inne_kryteria": ["Inne kryteria z wagami, jeśli istnieją"],
    "opis": "Jak dokładnie będą oceniane oferty"
  }},

  "kary_umowne": [
    "Każda kara umowna z KONKRETNĄ KWOTĄ lub PROCENTEM: za co, ile (% wynagrodzenia lub kwota zł), limit kar jeśli podany. Szukaj w Projekcie Umowy, SWZ i załącznikach. Jeśli kary są tylko w załączniku którego nie masz — napisz 'Kary umowne określone w [nazwa załącznika] — dokument niedostępny w analizowanych plikach'."
  ],

  "podsumowanie_statystyczne": {{
    "slupy": "Łączna liczba i masa (np. '61 kpl / 175,87 T'), z podziałem na strefy jeśli występuje (np. 'S1: 58, S2: 3')",
    "fundamenty": "Łączna liczba kompletów fundamentów zsumowana z WSZYSTKICH pozycji tabeli, z podziałem na tomy (np. 'TOM II-F-1: 24 kpl, TOM II-F-2: 37 kpl = 61 kpl łącznie')",
    "izolatory": "Łączna liczba łańcuchów izolatorowych PER TYP i SUMA (np. 'ŁP: 30, ŁP2: 55, ŁPm: 20, ŁO: 48, ŁO2: 72+3, ŁO bramka: 3, ŁPV: 15, ŁPV2/1: 26 = 272 kpl łącznie')",
    "przewody_fazowe_m": "Zsumowana długość przewodów fazowych w metrach — zsumuj WSZYSTKIE odcinki (np. '3768+11703+11901+7509+7176+9429+8619+1491 = 61 596 m')",
    "opgw_m": "Zsumowana długość OPGW w metrach — zsumuj WSZYSTKIE odcinki osobno",
    "tlumiki_drgan": "Tłumiki fazowe (szt/kpl) + tłumiki OPGW (szt/kpl) + oploty",
    "zawiesia": "Zsumowane PER TYP: przelotowe, odciągowe podwójne, odciągowe pojedyncze, rozgałęźne, itp.",
    "uziemienia": "Robocze: łączna liczba kompletów + materiały (bednarka m, pręty szt). Ochronne osobno.",
    "oznakowanie": "Tablice ostrzegawcze, numeracyjne, fazowe — każda z ilością",
    "inne": "Straszaki na ptaki, konstrukcje ADSS, osprzęt światłowodowy, malowanie — każde z ilością"
  }}
}}

KRYTYCZNE INSTRUKCJE:
1. MATERIAŁY — TO NAJWAŻNIEJSZA CZĘŚĆ BRIEFINGU:
   - Przeczytaj CAŁE zestawienie materiałów od pierwszej do ostatniej pozycji
   - Dla KAŻDEJ kategorii (I, II, III, IV...) podaj zsumowaną ilość główną
   - W podkategoriach wypisz KAŻDY typ/podtyp z ilością — nie pomijaj żadnej pozycji
   - ZSUMUJ odcinki przewodów (fazowych i OPGW) — podaj arytmetykę: np. "3768+11703+...=61596 m"
   - ZSUMUJ fundamenty z OBIE tabele (TOM II-F-1 i TOM II-F-2) osobno i łącznie
   - ZSUMUJ łańcuchy izolatorowe per typ (ŁP, ŁP2, ŁPm, ŁO, ŁO2, itp.) i łącznie
   - Nie pisz null jeśli dane SĄ w zestawieniu. Policz i podaj.
2. RYZYKA: Minimum 5 ryzyk. Każde ryzyko MUSI mieć odniesienie do dokumentu.
3. REKOMENDACJE: Minimum 5 rekomendacji. Każda MUSI być konkretna i actionable.
4. ZMIANY: Wylistuj WSZYSTKIE zmiany z odpowiedzi na pytania. Zaznacz też odmowy.
5. KARY: Wylistuj WSZYSTKIE kary umowne z konkretnymi kwotami/procentami — to krytyczne dla wyceny. Jeśli kary są w niedostępnym załączniku, WYRAŹNIE to zaznacz.
6. NIE WYMYŚLAJ danych których nie ma w dokumentach. Lepiej wpisać null niż zgadywać.
7. LINK: Jeśli w dokumentach jest URL do platformy przetargowej (np. swpp2.gkpge.pl), PODAJ GO.
8. IDENTYFIKATORY: Podaj PM i PSP jeśli występują w dokumentach.
9. LOKALIZACJA: Wyodrębnij WSZYSTKIE gminy, powiaty, województwa i punkty charakterystyczne (GPZ, stacje). Szukaj w OPZ i SWZ.
10. PROJEKT: Zidentyfikuj firmę projektową (szukaj w OPZ, często w pkt 1.x) i określ czy projekt jest gotowy czy do wykonania.
11. TRYB PRAWNY: Ustal czy postępowanie jest na PZP czy regulaminie wewnętrznym — szukaj na pierwszych stronach SWZ (nagłówek, podstawa prawna).

Zwróć WYŁĄCZNIE JSON, bez żadnego tekstu przed ani po."""

# The instruction-only part for PDF native mode
# Extract from template and unescape double braces ({{ → {, }} → })
_INSTRUCTION_START = "Zwróć JSON o następującej strukturze"
_instruction_part = USER_PROMPT_TEMPLATE.split(_INSTRUCTION_START, 1)[1]
_raw_pdf_prompt = (
    "Przeanalizuj załączone dokumenty przetargowe (PDF) i zwróć ustrukturyzowany JSON.\n\n"
    + _INSTRUCTION_START
    + _instruction_part
)
# Template uses {{ }} for literal braces — PDF mode doesn't go through .format() so unescape
PDF_USER_PROMPT = _raw_pdf_prompt.replace("{{", "{").replace("}}", "}")


def build_user_prompt(documents_text: str) -> str:
    """Build the user prompt with embedded extracted text (fallback mode)."""
    return USER_PROMPT_TEMPLATE.format(documents_text=documents_text)


def build_pdf_user_prompt() -> str:
    """Build the user prompt for native PDF mode (no text embedding needed)."""
    return PDF_USER_PROMPT
