import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// Palet warna bernuansa batik tradisional nusantara:
/// soga (coklat), nila (biru indigo), marun, krem, dan kertas.
class BatikColors {
  BatikColors._();

  static const soga = Color(0xFF5B3A1E);
  static const sogaGelap = Color(0xFF2E1B0E);
  static const sogaMuda = Color(0xFF8B5A2B);
  static const nila = Color(0xFF1E2A4A);
  static const krem = Color(0xFFF5EFEB);
  static const kertas = Color(0xFFFAF7F2);
  static const putih = Color(0xFFFFFFFF);
  static const marun = Color(0xFF8B251E);
  static const marunGelap = Color(0xFF5E130D);
  static const emas = Color(0xFFC8962E);
  static const hijau = Color(0xFF355E3B);
  static const garis = Color(0xFFE5DAC9);
  static const abuMuda = Color(0xFFEAEAEA);
  static const abuTeks = Color(0xFF6B7280);
}

ThemeData buildBatikTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: BatikColors.soga,
    brightness: Brightness.light,
  ).copyWith(
    primary: BatikColors.marun,
    onPrimary: Colors.white,
    secondary: BatikColors.soga,
    onSecondary: Colors.white,
    tertiary: BatikColors.nila,
    surface: BatikColors.putih,
    onSurface: BatikColors.sogaGelap,
  );

  final base = ThemeData(useMaterial3: true, colorScheme: scheme);

  final textTheme = GoogleFonts.nunitoSansTextTheme(base.textTheme).apply(
    bodyColor: BatikColors.sogaGelap,
    displayColor: BatikColors.sogaGelap,
  );

  TextStyle heading(TextStyle? style) {
    return GoogleFonts.playfairDisplay(
      textStyle: style,
      fontWeight: FontWeight.w700,
      color: BatikColors.sogaGelap,
    );
  }

  return base.copyWith(
    scaffoldBackgroundColor: BatikColors.kertas,
    dividerColor: BatikColors.garis,
    appBarTheme: const AppBarTheme(
      backgroundColor: Colors.white,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      iconTheme: IconThemeData(color: BatikColors.sogaGelap),
    ),
    textTheme: textTheme.copyWith(
      headlineLarge: heading(textTheme.headlineLarge),
      headlineMedium: heading(textTheme.headlineMedium),
      headlineSmall: heading(textTheme.headlineSmall),
      titleLarge: heading(textTheme.titleLarge),
      titleMedium: GoogleFonts.playfairDisplay(
        textStyle: textTheme.titleMedium,
        fontWeight: FontWeight.w600,
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: BatikColors.marun,
        foregroundColor: Colors.white,
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 14),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
        textStyle: const TextStyle(fontWeight: FontWeight.w700, letterSpacing: 0.3),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: BatikColors.marun,
        side: const BorderSide(color: BatikColors.marun),
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
        textStyle: const TextStyle(fontWeight: FontWeight.w700),
      ),
    ),
    cardTheme: CardThemeData(
      color: Colors.white,
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(10),
        side: const BorderSide(color: BatikColors.garis),
      ),
    ),
    snackBarTheme: SnackBarThemeData(
      behavior: SnackBarBehavior.floating,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      backgroundColor: BatikColors.sogaGelap,
      contentTextStyle: const TextStyle(color: Colors.white, fontSize: 13.5),
      elevation: 4,
      insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
    ),
  );
}

