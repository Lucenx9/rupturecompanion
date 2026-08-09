#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif

#include "live_context.h"

#include "plugin_interface.h"

#include "AuCrafting_structs.hpp"
#include "AuItems_classes.hpp"
#include "Chimera_classes.hpp"
#include "Chimera_structs.hpp"
#include "Engine_classes.hpp"

#include <algorithm>
#include <atomic>
#include <bit>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <iomanip>
#include <iterator>
#include <locale>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <windows.h>
#include <winver.h>

namespace RuptureCompanion::LiveContext
{
namespace
{
using Fields = std::vector<std::pair<std::string, std::string>>;

using SteadyClock = std::chrono::steady_clock;

constexpr auto SampleInterval = std::chrono::milliseconds(750);
constexpr auto WorldProbeInterval = std::chrono::seconds(2);
constexpr int MaxInventorySlots = 256;
constexpr int MaxInventoryItems = 128;
constexpr int MaxReplicatedPlayers = 8;
constexpr int MaxObjectives = 32;
constexpr int MaxSubObjectives = 64;
constexpr int MaxInteractedItems = 32;
constexpr int MaxTechnologyEntries = 64;
constexpr int MaxTextCodeUnits = 4096;
constexpr std::size_t MaxStringBytes = 128;
constexpr std::size_t MaxSnapshotBytes = 48 * 1024;

using FreeEngineMemoryFunction = void (*)(void*);

IPluginSelf* g_self = nullptr;
SDK::UWorld* g_world = nullptr;
SteadyClock::time_point g_nextSampleAt{};
SteadyClock::time_point g_nextWorldProbeAt{};
std::mutex g_snapshotMutex;
std::string g_snapshot;
bool g_registered = false;
std::atomic<DWORD> g_gameThreadId{0};
std::atomic<DWORD> g_registrationThreadId{0};
std::atomic_bool g_registeredDuringStartup{false};
HANDLE g_cleanupEvent = nullptr;
FreeEngineMemoryFunction g_freeEngineMemory = nullptr;
bool g_loggedSuccessfulSample = false;

BOOL CALLBACK FindWindowOwnedByCurrentThread(HWND window, LPARAM context)
{
    auto* found = reinterpret_cast<bool*>(context);
    DWORD processId = 0;
    const DWORD windowThreadId = GetWindowThreadProcessId(window, &processId);
    wchar_t className[64]{};
    GetClassNameW(window, className, static_cast<int>(std::size(className)));
    if (processId == GetCurrentProcessId()
        && windowThreadId == GetCurrentThreadId()
        && IsWindowVisible(window) && GetWindow(window, GW_OWNER) == nullptr)
    {
        // The startup worker owns the Mod Loader splash, not the game window.
        if (lstrcmpW(className, L"StarRuptureModLoaderSplash") == 0)
        {
            return TRUE;
        }
        *found = true;
        return FALSE;
    }
    return TRUE;
}

bool CurrentThreadOwnsProcessWindow()
{
    bool found = false;
    EnumWindows(&FindWindowOwnedByCurrentThread,
        reinterpret_cast<LPARAM>(&found));
    return found;
}

bool CompatibleGameBuild()
{
    // Extended-length Win32 paths can be much longer than MAX_PATH. A return
    // value equal to the buffer capacity means GetModuleFileNameW truncated it.
    constexpr DWORD MaxExtendedPath = 32768;
    std::vector<wchar_t> executablePath(MaxExtendedPath);
    const DWORD executablePathLength = GetModuleFileNameW(
        nullptr, executablePath.data(), MaxExtendedPath);
    if (executablePathLength == 0 || executablePathLength >= MaxExtendedPath)
    {
        return false;
    }
    DWORD unused = 0;
    const DWORD infoSize = GetFileVersionInfoSizeW(executablePath.data(), &unused);
    if (infoSize == 0)
    {
        return false;
    }
    std::vector<std::byte> info(infoSize);
    if (!GetFileVersionInfoW(
            executablePath.data(), 0, infoSize, info.data()))
    {
        return false;
    }

    struct LanguageCodepage
    {
        WORD language;
        WORD codepage;
    };
    LanguageCodepage* translations = nullptr;
    UINT translationBytes = 0;
    VerQueryValueW(info.data(), L"\\VarFileInfo\\Translation",
        reinterpret_cast<void**>(&translations), &translationBytes);

    wchar_t query[64]{};
    if (translations != nullptr && translationBytes >= sizeof(LanguageCodepage))
    {
        swprintf_s(query, L"\\StringFileInfo\\%04x%04x\\ProductVersion",
            translations[0].language, translations[0].codepage);
    }
    else
    {
        wcscpy_s(query, L"\\StringFileInfo\\040904b0\\ProductVersion");
    }

    wchar_t* productVersion = nullptr;
    UINT productVersionLength = 0;
    if (!VerQueryValueW(info.data(), query,
            reinterpret_cast<void**>(&productVersion), &productVersionLength)
        || productVersion == nullptr || productVersionLength == 0)
    {
        return false;
    }
    constexpr std::wstring_view RequiredSuffix = L"CL-121391";
    const std::wstring_view actual(productVersion, productVersionLength - 1);
    return actual.ends_with(RequiredSuffix);
}

std::string TruncateUtf8(std::string value, const std::size_t limit = MaxStringBytes)
{
    if (value.size() <= limit)
    {
        return value;
    }
    std::size_t end = limit;
    while (end > 0
           && (static_cast<unsigned char>(value[end]) & 0xC0U) == 0x80U)
    {
        --end;
    }
    value.resize(end);
    return value;
}

std::string JsonString(const std::string_view value)
{
    std::string output;
    output.reserve(value.size() + 2);
    output.push_back('"');
    constexpr char Hex[] = "0123456789abcdef";
    for (const unsigned char character : value)
    {
        switch (character)
        {
        case '"':
            output += "\\\"";
            break;
        case '\\':
            output += "\\\\";
            break;
        case '\b':
            output += "\\b";
            break;
        case '\f':
            output += "\\f";
            break;
        case '\n':
            output += "\\n";
            break;
        case '\r':
            output += "\\r";
            break;
        case '\t':
            output += "\\t";
            break;
        default:
            if (character < 0x20U)
            {
                output += "\\u00";
                output.push_back(Hex[(character >> 4U) & 0x0FU]);
                output.push_back(Hex[character & 0x0FU]);
            }
            else
            {
                output.push_back(static_cast<char>(character));
            }
        }
    }
    output.push_back('"');
    return output;
}

std::string JsonNumber(const double value)
{
    if (!std::isfinite(value))
    {
        return "null";
    }
    std::ostringstream output;
    output.imbue(std::locale::classic());
    output << std::fixed << std::setprecision(3) << value;
    std::string result = output.str();
    while (result.size() > 1 && result.back() == '0')
    {
        result.pop_back();
    }
    if (!result.empty() && result.back() == '.')
    {
        result.pop_back();
    }
    return result == "-0" ? "0" : result;
}

std::string JsonInteger(const std::int64_t value)
{
    return std::to_string(value);
}

std::string JsonBoolean(const bool value)
{
    return value ? "true" : "false";
}

std::string JsonObject(const Fields& fields)
{
    std::string output = "{";
    bool first = true;
    for (const auto& [key, value] : fields)
    {
        if (!first)
        {
            output.push_back(',');
        }
        first = false;
        output += JsonString(key);
        output.push_back(':');
        output += value;
    }
    output.push_back('}');
    return output;
}

std::string JsonArray(const std::vector<std::string>& values)
{
    std::string output = "[";
    bool first = true;
    for (const std::string& value : values)
    {
        if (!first)
        {
            output.push_back(',');
        }
        first = false;
        output += value;
    }
    output.push_back(']');
    return output;
}

void Add(Fields& fields, const std::string_view key, std::string value)
{
    fields.emplace_back(std::string(key), std::move(value));
}

void AddNumber(Fields& fields, const std::string_view key, const double value)
{
    if (std::isfinite(value))
    {
        Add(fields, key, JsonNumber(value));
    }
}

void AddSectionName(std::vector<std::string>& sections, const std::string_view name)
{
    std::string encoded = JsonString(name);
    if (std::ranges::find(sections, encoded) == sections.end())
    {
        sections.push_back(std::move(encoded));
    }
}

std::int64_t UnixTimeMilliseconds()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::string SafeText(const SDK::FText& text)
{
    if (text.TextData == nullptr || g_freeEngineMemory == nullptr)
    {
        return {};
    }

    SDK::FString converted =
        SDK::UKismetTextLibrary::Conv_TextToString(text);
    const wchar_t* data = converted.GetDataPtr();
    struct EngineStringBuffer
    {
        const wchar_t* data;
        ~EngineStringBuffer()
        {
            if (data != nullptr)
            {
                g_freeEngineMemory(const_cast<wchar_t*>(data));
            }
        }
    } buffer{data};

    const int length = converted.Num();
    if (data == nullptr || length <= 1 || converted.Max() < length
        || length > MaxTextCodeUnits)
    {
        return {};
    }
    return TruncateUtf8(converted.ToString());
}

std::string ItemName(const SDK::UAuItemDataBase* item)
{
    if (item == nullptr)
    {
        return {};
    }
    std::string name = SafeText(item->ItemName);
    if (name.empty())
    {
        name = TruncateUtf8(item->UniqueItemName.ToString());
    }
    if (name.empty())
    {
        name = TruncateUtf8(item->GetName());
    }
    return name;
}

std::string ObjectName(const SDK::UObject* object)
{
    return object == nullptr ? std::string{} : TruncateUtf8(object->GetName());
}

std::string ClassName(const SDK::UObject* object)
{
    return object == nullptr || object->Class == nullptr
        ? std::string{}
        : TruncateUtf8(object->Class->GetName());
}

bool ValidGuid(const SDK::FGuid& guid)
{
    return guid.A != 0 || guid.B != 0 || guid.C != 0 || guid.D != 0;
}

bool SameGuid(const SDK::FGuid& left, const SDK::FGuid& right)
{
    return left.A == right.A && left.B == right.B && left.C == right.C
        && left.D == right.D;
}

std::string GuidString(const SDK::FGuid& guid)
{
    std::ostringstream output;
    output << std::hex << std::setfill('0') << std::setw(8)
           << static_cast<std::uint32_t>(guid.A) << '-' << std::setw(8)
           << static_cast<std::uint32_t>(guid.B) << '-' << std::setw(8)
           << static_cast<std::uint32_t>(guid.C) << '-' << std::setw(8)
           << static_cast<std::uint32_t>(guid.D);
    return output.str();
}

const char* ObjectiveStateName(const SDK::ECrObjectiveState state)
{
    switch (state)
    {
    case SDK::ECrObjectiveState::Inactive:
        return "inactive";
    case SDK::ECrObjectiveState::Active:
        return "active";
    case SDK::ECrObjectiveState::Completed:
        return "completed";
    default:
        return "unknown";
    }
}

const char* WaveTypeName(const SDK::EEnviroWave type)
{
    switch (type)
    {
    case SDK::EEnviroWave::None:
        return "none";
    case SDK::EEnviroWave::Heat:
        return "heat";
    case SDK::EEnviroWave::Cold:
        return "cold";
    default:
        return "unknown";
    }
}

const char* WaveStageName(const SDK::EEnviroWaveStage stage)
{
    switch (stage)
    {
    case SDK::EEnviroWaveStage::None:
        return "none";
    case SDK::EEnviroWaveStage::PreWave:
        return "pre_wave";
    case SDK::EEnviroWaveStage::Moving:
        return "moving";
    case SDK::EEnviroWaveStage::Fadeout:
        return "fadeout";
    case SDK::EEnviroWaveStage::Growback:
        return "growback";
    default:
        return "unknown";
    }
}

const char* BuildingStateName(const SDK::ECrBuildingState state)
{
    switch (state)
    {
    case SDK::ECrBuildingState::Unknown:
        return "unknown";
    case SDK::ECrBuildingState::NoElectricity:
        return "no_electricity";
    case SDK::ECrBuildingState::Working:
        return "working";
    case SDK::ECrBuildingState::Inefficient:
        return "inefficient";
    case SDK::ECrBuildingState::Blocked:
        return "blocked";
    case SDK::ECrBuildingState::NotWorking:
        return "not_working";
    default:
        return "unknown";
    }
}

const char* GridConnectionName(const SDK::ECrBuildingGridConnectionStatus status)
{
    switch (status)
    {
    case SDK::ECrBuildingGridConnectionStatus::NotConnected:
        return "not_connected";
    case SDK::ECrBuildingGridConnectionStatus::Connected:
        return "connected";
    case SDK::ECrBuildingGridConnectionStatus::ConnectedAndOff:
        return "connected_off";
    default:
        return "unknown";
    }
}

const char* AlienActivityName(const SDK::EHUDAlienActivityType type)
{
    switch (type)
    {
    case SDK::EHUDAlienActivityType::AlienSignaturesDetected:
        return "alien_signatures_detected";
    case SDK::EHUDAlienActivityType::NoHostileActivity:
        return "no_hostile_activity";
    case SDK::EHUDAlienActivityType::SignalLost:
        return "signal_lost";
    case SDK::EHUDAlienActivityType::None:
        return "none";
    default:
        return "unknown";
    }
}

const char* SkillName(const SDK::ECrPlayerProgressionSkill skill)
{
    switch (skill)
    {
    case SDK::ECrPlayerProgressionSkill::Movement:
        return "movement";
    case SDK::ECrPlayerProgressionSkill::Combat:
        return "combat";
    case SDK::ECrPlayerProgressionSkill::Survival:
        return "survival";
    case SDK::ECrPlayerProgressionSkill::None:
        return "none";
    default:
        return "unknown";
    }
}

const char* ProfessionName(const SDK::EProfessionType profession)
{
    switch (profession)
    {
    case SDK::EProfessionType::Engineer:
        return "engineer";
    case SDK::EProfessionType::Soldier:
        return "soldier";
    case SDK::EProfessionType::Medic:
        return "medic";
    case SDK::EProfessionType::Scientist:
        return "scientist";
    case SDK::EProfessionType::NONE:
        return "none";
    default:
        return "unknown";
    }
}

void AddUnlockedFeature(
    std::vector<std::string>& features,
    const SDK::ECrCorporationUnlockedFeatures flags,
    const SDK::ECrCorporationUnlockedFeatures feature,
    const std::string_view name)
{
    const auto rawFlags = static_cast<std::uint8_t>(flags);
    const auto rawFeature = static_cast<std::uint8_t>(feature);
    if ((rawFlags & rawFeature) != 0U)
    {
        features.push_back(JsonString(name));
    }
}

std::string AttributeJson(
    const SDK::FGameplayAttributeData& current,
    const SDK::FGameplayAttributeData& minimum,
    const SDK::FGameplayAttributeData& maximum)
{
    Fields fields;
    AddNumber(fields, "current", current.CurrentValue);
    AddNumber(fields, "min", minimum.CurrentValue);
    AddNumber(fields, "max", maximum.CurrentValue);
    return JsonObject(fields);
}

std::string BuildVitals(const SDK::ACrCharacterPlayerBase* player)
{
    Fields fields;
    if (player->HealthAttributes != nullptr)
    {
        const auto* value = player->HealthAttributes;
        Add(fields, "health", AttributeJson(
            value->CurrentHealth, value->MinHealth, value->MaxHealth));
    }
    if (player->EnergyAttributes != nullptr)
    {
        const auto* value = player->EnergyAttributes;
        Add(fields, "energy", AttributeJson(
            value->CurrentEnergy, value->MinEnergy, value->MaxEnergy));
    }
    if (player->ShieldAttributes != nullptr)
    {
        const auto* value = player->ShieldAttributes;
        Add(fields, "shield", AttributeJson(
            value->CurrentShield, value->MinShield, value->MaxShield));
    }
    if (player->OxygenAttributes != nullptr)
    {
        const auto* value = player->OxygenAttributes;
        Add(fields, "oxygen", AttributeJson(
            value->CurrentOxygen, value->MinOxygen, value->MaxOxygen));
    }
    if (player->HydrationAttributes != nullptr)
    {
        const auto* value = player->HydrationAttributes;
        Add(fields, "hydration", AttributeJson(
            value->CurrentHydration, value->MinHydration, value->MaxHydration));
    }
    if (player->CaloriesAttributes != nullptr)
    {
        const auto* value = player->CaloriesAttributes;
        Add(fields, "calories", AttributeJson(
            value->CurrentCalories, value->MinCalories, value->MaxCalories));
    }
    if (player->ToxicityAttributes != nullptr)
    {
        const auto* value = player->ToxicityAttributes;
        Add(fields, "toxicity", AttributeJson(
            value->CurrentToxicity, value->MinToxicity, value->MaxToxicity));
    }
    if (player->RadiationAttributes != nullptr)
    {
        const auto* value = player->RadiationAttributes;
        Add(fields, "radiation", AttributeJson(
            value->CurrentRadiation, value->MinRadiation, value->MaxRadiation));
    }
    if (player->HeatAttributes != nullptr)
    {
        const auto* value = player->HeatAttributes;
        Add(fields, "heat", AttributeJson(
            value->CurrentHeat, value->MinHeat, value->MaxHeat));
    }
    if (player->DrainAttributes != nullptr)
    {
        const auto* value = player->DrainAttributes;
        Add(fields, "drain", AttributeJson(
            value->CurrentDrain, value->MinDrain, value->MaxDrain));
    }
    if (player->CorrosionAttributes != nullptr)
    {
        const auto* value = player->CorrosionAttributes;
        Add(fields, "corrosion", AttributeJson(
            value->CurrentCorrosion, value->MinCorrosion, value->MaxCorrosion));
    }
    if (player->InfectionAttributes != nullptr)
    {
        const auto* value = player->InfectionAttributes;
        Add(fields, "infection", AttributeJson(
            value->CurrentInfection, value->MinInfection, value->MaxInfection));
    }
    if (player->TemperatureAttributes != nullptr)
    {
        const auto* value = player->TemperatureAttributes;
        Add(fields, "temperature", AttributeJson(
            value->CurrentTemperature, value->MinTemperature, value->MaxTemperature));
    }
    if (player->MedToolChargeAttributes != nullptr)
    {
        const auto* value = player->MedToolChargeAttributes;
        Add(fields, "med_tool_charge", AttributeJson(
            value->CurrentMedToolCharge,
            value->MinMedToolCharge,
            value->MaxMedToolCharge));
    }
    if (player->GrenadeChargeAttributes != nullptr)
    {
        const auto* value = player->GrenadeChargeAttributes;
        Add(fields, "grenade_charge", AttributeJson(
            value->CurrentGrenadeCharge,
            value->MinGrenadeCharge,
            value->MaxGrenadeCharge));
    }
    if (player->MovementSpeedMultiplierAttributes != nullptr)
    {
        const auto* value = player->MovementSpeedMultiplierAttributes;
        Add(fields, "movement_speed_multiplier", AttributeJson(
            value->CurrentMovementSpeedMultiplier,
            value->MinMovementSpeedMultiplier,
            value->MaxMovementSpeedMultiplier));
    }
    return JsonObject(fields);
}

std::string BuildItemContainer(
    SDK::UCrInventoryComponent* inventory,
    SDK::UCrInventoryItemsStoreComponent* store,
    bool& truncated,
    bool& available)
{
    if (inventory == nullptr || store == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    const int slotCount = inventory->Slots.Num();
    const int safeSlotCount = std::clamp(slotCount, 0, MaxInventorySlots);
    truncated = slotCount > MaxInventorySlots;
    std::set<std::string> seenItemIds;
    for (int index = 0; index < safeSlotCount; ++index)
    {
        if (!inventory->Slots.IsValidIndex(index))
        {
            continue;
        }
        const SDK::FCrInventorySlot& slot = inventory->Slots[index];
        if (!ValidGuid(slot.ItemId.Handle))
        {
            continue;
        }
        seenItemIds.insert(GuidString(slot.ItemId.Handle));
    }

    std::map<std::string, std::int64_t> itemAmounts;
    std::set<std::string> seenItemTypes;
    const auto& storeItems = store->ItemsArray.Items;
    const int storeItemCount = std::clamp(storeItems.Num(), 0, MaxInventorySlots);
    truncated = truncated || storeItems.Num() > MaxInventorySlots;
    for (int index = 0; index < storeItemCount; ++index)
    {
        if (!storeItems.IsValidIndex(index))
        {
            continue;
        }
        const SDK::UAuItemDataBase* item = storeItems[index].ItemTypeGCHolder;
        if (item == nullptr)
        {
            continue;
        }
        std::string identity = TruncateUtf8(item->UniqueItemName.ToString());
        if (identity.empty())
        {
            identity = ObjectName(item);
        }
        std::string name = ItemName(item);
        if (identity.empty() || name.empty() || !seenItemTypes.insert(identity).second)
        {
            continue;
        }
        const int amount = inventory->BP_GetItemAmount(item);
        if (amount > 0)
        {
            itemAmounts[std::move(name)] += amount;
        }
    }

    std::vector<std::string> items;
    for (const auto& [name, amount] : itemAmounts)
    {
        if (items.size() >= MaxInventoryItems)
        {
            truncated = true;
            break;
        }
        Fields itemFields;
        Add(itemFields, "name", JsonString(name));
        Add(itemFields, "amount", JsonInteger(amount));
        items.push_back(JsonObject(itemFields));
    }
    Fields fields;
    Add(fields, "grid_columns", JsonInteger(inventory->GridColumns));
    Add(fields, "grid_rows", JsonInteger(inventory->GridRows));
    Add(fields, "capacity_cells", JsonInteger(
            std::max(0, inventory->GridColumns) * std::max(0, inventory->GridRows)));
    Add(fields, "item_stack_count", JsonInteger(
            static_cast<std::int64_t>(seenItemIds.size())));
    Add(fields, "items", JsonArray(items));
    Add(fields, "truncated", JsonBoolean(truncated));
    return JsonObject(fields);
}

std::string BuildInventory(
    SDK::ACrCharacterPlayerBase* player,
    bool& truncated,
    bool& available)
{
    return BuildItemContainer(
        player->InventoryComponent,
        player->InventoryItemsStore,
        truncated,
        available);
}

std::string BuildGems(
    SDK::ACrCharacterPlayerBase* player,
    bool& truncated,
    bool& available)
{
    return BuildItemContainer(
        player->InventoryGemsComponent,
        player->GemItemsStore,
        truncated,
        available);
}

std::string BuildEquipment(SDK::ACrCharacterPlayerBase* player)
{
    Fields fields;
    const SDK::UCrWeaponItemDataBase* weapon = player->GetEquippedWeaponItemDataBase();
    if (weapon != nullptr)
    {
        Add(fields, "weapon", JsonString(ItemName(weapon)));
    }
    SDK::UCrWeaponComponent* component = player->BP_GetWeaponComponent();
    if (component != nullptr && weapon != nullptr)
    {
        AddNumber(fields, "ammo_loaded", component->GetEquippedWeaponCurrentAmmo());
        AddNumber(fields, "ammo_capacity", component->GetEquippedWeaponMaxAmmo());
        AddNumber(fields, "ammo_in_inventory", component->GetEquippedWeaponAmmoInInventory());
        Add(fields, "firing", JsonBoolean(component->IsFireInputDown()));
    }
    return JsonObject(fields);
}

std::vector<std::string> TechnologyNames(
    const SDK::TArray<SDK::UCrBuildingData*>& entries,
    bool& truncated,
    bool& textConverted)
{
    std::vector<std::string> names;
    std::set<std::string> seen;
    const int count = std::min(entries.Num(), MaxTechnologyEntries);
    truncated = truncated || entries.Num() > MaxTechnologyEntries;
    for (int index = 0; index < count; ++index)
    {
        if (!entries.IsValidIndex(index) || entries[index] == nullptr)
        {
            continue;
        }
        std::string name = SafeText(entries[index]->BuildingName);
        textConverted = textConverted
            || entries[index]->BuildingName.TextData != nullptr;
        if (name.empty())
        {
            name = ObjectName(entries[index]);
        }
        if (!name.empty() && seen.insert(name).second)
        {
            names.push_back(JsonString(name));
        }
    }
    return names;
}

std::vector<std::string> TechnologyNames(
    const SDK::TArray<SDK::UCrItemRecipeData*>& entries,
    bool& truncated,
    bool& textConverted)
{
    std::vector<std::string> names;
    std::set<std::string> seen;
    const int count = std::min(entries.Num(), MaxTechnologyEntries);
    truncated = truncated || entries.Num() > MaxTechnologyEntries;
    for (int index = 0; index < count; ++index)
    {
        if (!entries.IsValidIndex(index) || entries[index] == nullptr)
        {
            continue;
        }
        std::string name = SafeText(entries[index]->DisplayText);
        textConverted = textConverted
            || entries[index]->DisplayText.TextData != nullptr;
        if (name.empty())
        {
            name = ObjectName(entries[index]);
        }
        if (!name.empty() && seen.insert(name).second)
        {
            names.push_back(JsonString(name));
        }
    }
    return names;
}

std::string BuildProgression(
    SDK::ACrGameStateBase* gameState,
    SDK::ACrPlayerControllerBase* controller,
    bool& available,
    bool& truncated,
    bool& technologyTextConverted)
{
    if (gameState == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    Fields fields;
    AddNumber(fields, "playtime_seconds", gameState->PlaytimeDuration);
    Add(fields, "tutorial_completed", JsonBoolean(gameState->bTutorialCompleted));
    Add(fields, "in_tutorial", JsonBoolean(gameState->IsInTutorial()));

    if (controller != nullptr)
    {
        std::vector<std::string> skills;
        const int skillCount = std::min(controller->PlayerSkills.Num(), 16);
        truncated = truncated || controller->PlayerSkills.Num() > 16;
        for (int index = 0; index < skillCount; ++index)
        {
            if (!controller->PlayerSkills.IsValidIndex(index))
            {
                continue;
            }
            const SDK::FCrSkillData& skill = controller->PlayerSkills[index];
            Fields skillFields;
            Add(skillFields, "name", JsonString(SkillName(skill.Skill)));
            Add(skillFields, "level", JsonInteger(skill.Level));
            AddNumber(skillFields, "experience", skill.Experience);
            skills.push_back(JsonObject(skillFields));
        }
        Add(fields, "skills", JsonArray(skills));
    }
    else
    {
        Add(fields, "skills", "null");
    }

    SDK::ACrCorporationsOwner* corporations = gameState->CorporationsOwner;
    if (corporations != nullptr)
    {
        Add(fields, "data_points", JsonInteger(corporations->DataPoints));
        Add(fields, "unlocked_inventory_slots", JsonInteger(
                corporations->GetUnlockedInventorySlotsNumber()));
        std::vector<std::string> unlockedFeatures;
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::Map, "map");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::Grenade, "grenade");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::MedTool, "med_tool");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::BuildingDrone, "building_drone");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::Pistol, "pistol");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::BiggerInventory, "bigger_inventory");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::Dash, "dash");
        Add(fields, "unlocked_features", JsonArray(unlockedFeatures));

        std::vector<std::string> corporationEntries;
        const auto& corporationData = corporations->CorporationsContainer.CorporationsData;
        const int corporationCount = std::min(corporationData.Num(), 16);
        truncated = truncated || corporationData.Num() > 16;
        for (int index = 0; index < corporationCount; ++index)
        {
            if (!corporationData.IsValidIndex(index) || corporationData[index].bHidden)
            {
                continue;
            }
            const SDK::FCrCorporation& corporation = corporationData[index];
            Fields corporationFields;
            Add(corporationFields, "name", JsonString(
                    TruncateUtf8(corporation.Name.ToString())));
            Add(corporationFields, "level", JsonInteger(corporation.Level));
            Add(corporationFields, "reputation", JsonInteger(corporation.Reputation));
            Add(corporationFields, "research_points_tier_1", JsonInteger(
                    corporation.ResearchPointsTier1));
            Add(corporationFields, "research_points_tier_2", JsonInteger(
                    corporation.ResearchPointsTier2));
            corporationEntries.push_back(JsonObject(corporationFields));
        }
        Add(fields, "corporations", JsonArray(corporationEntries));
    }
    else
    {
        Add(fields, "corporations", "null");
    }

    SDK::ACrTechnologyKeeper* technology = gameState->TechnologyKeeper;
    if (technology != nullptr)
    {
        Fields technologyFields;
        Add(technologyFields, "available_buildings_count", JsonInteger(
                std::max(0, technology->AvailableBuildings.Num())));
        Add(technologyFields, "replicated_recipes_count", JsonInteger(
                std::max(0, technology->AllRecipes.Num())));
        Add(technologyFields, "available_buildings", JsonArray(
                TechnologyNames(technology->AvailableBuildings, truncated,
                    technologyTextConverted)));
        Add(technologyFields, "replicated_recipes", JsonArray(
                TechnologyNames(technology->AllRecipes, truncated,
                    technologyTextConverted)));
        Add(fields, "technology", JsonObject(technologyFields));
    }
    else
    {
        Add(fields, "technology", "null");
    }

    std::vector<std::string> missingSubsections;
    if (controller == nullptr)
    {
        missingSubsections.push_back(JsonString("skills"));
    }
    if (corporations == nullptr)
    {
        missingSubsections.push_back(JsonString("corporations"));
    }
    if (technology == nullptr)
    {
        missingSubsections.push_back(JsonString("technology"));
    }
    Add(fields, "partial", JsonBoolean(!missingSubsections.empty()));
    Add(fields, "missing_subsections", JsonArray(missingSubsections));
    Add(fields, "truncated", JsonBoolean(truncated));
    return JsonObject(fields);
}

std::string BuildSession(
    SDK::ACrGameStateBase* gameState,
    SDK::ACrCharacterPlayerBase* localPlayer,
    bool& available,
    bool& truncated)
{
    if (gameState == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    Fields fields;
    Add(fields, "player_count", JsonInteger(std::max(0, gameState->PlayerArray.Num())));
    Add(fields, "match_started", JsonBoolean(gameState->HasMatchStarted()));
    Add(fields, "match_ended", JsonBoolean(gameState->HasMatchEnded()));
    Add(fields, "paused", JsonBoolean(gameState->bIsGamePaused));
    Add(fields, "cutscene_active", JsonBoolean(gameState->IsCutsceneActive()));
    AddNumber(fields, "server_world_time_seconds", gameState->GetServerWorldTimeSeconds());

    struct ReplicatedPlayer
    {
        double distanceMeters;
        SDK::ACrPlayerStateBase* state;
    };
    std::vector<ReplicatedPlayer> replicatedPlayers;
    const int playerCount = std::clamp(gameState->PlayerArray.Num(), 0, 64);
    truncated = gameState->PlayerArray.Num() > 64;
    const SDK::FVector localPosition = localPlayer->K2_GetActorLocation();
    for (int index = 0; index < playerCount; ++index)
    {
        if (!gameState->PlayerArray.IsValidIndex(index))
        {
            continue;
        }
        SDK::APlayerState* baseState = gameState->PlayerArray[index];
        if (baseState == nullptr
            || !baseState->IsA(SDK::ACrPlayerStateBase::StaticClass()))
        {
            continue;
        }
        auto* state = static_cast<SDK::ACrPlayerStateBase*>(baseState);
        if (state->CrChar == nullptr || state->CrChar == localPlayer)
        {
            continue;
        }
        replicatedPlayers.push_back({
            localPosition.GetDistanceToInMeters(state->CrChar->K2_GetActorLocation()),
            state});
    }
    std::ranges::sort(replicatedPlayers, {}, &ReplicatedPlayer::distanceMeters);
    if (replicatedPlayers.size() > MaxReplicatedPlayers)
    {
        replicatedPlayers.resize(MaxReplicatedPlayers);
        truncated = true;
    }
    std::vector<std::string> replicatedEntries;
    replicatedEntries.reserve(replicatedPlayers.size());
    for (const ReplicatedPlayer& entry : replicatedPlayers)
    {
        Fields replicatedFields;
        AddNumber(replicatedFields, "distance_m", entry.distanceMeters);
        Add(replicatedFields, "profession", JsonString(ProfessionName(entry.state->Profession)));
        Add(replicatedFields, "dead", JsonBoolean(entry.state->bDead));
        Add(replicatedFields, "incapacitated", JsonBoolean(entry.state->bIncapacitated));
        replicatedEntries.push_back(JsonObject(replicatedFields));
    }
    Add(fields, "closest_replicated_players", JsonArray(replicatedEntries));
    Add(fields, "replicated_players_truncated", JsonBoolean(truncated));
    return JsonObject(fields);
}

const SDK::FCrObjectiveData* FindObjectiveDefinition(const SDK::FGuid& id)
{
    SDK::UCrObjectivesDeveloperSettings* settings =
        SDK::UCrObjectivesDeveloperSettings::GetDefaultObj();
    SDK::UDataTable* table = settings == nullptr
        ? nullptr
        : settings->ObjectiveEntriesDefinition.Get();
    if (table == nullptr || !table->RowMap.IsValid() || table->RowMap.Num() > 1024)
    {
        return nullptr;
    }
    for (int index = 0; index < table->RowMap.Max(); ++index)
    {
        if (!table->RowMap.IsValidIndex(index))
        {
            continue;
        }
        const std::uint8_t* row = table->RowMap[index].Value();
        if (row == nullptr)
        {
            continue;
        }
        const auto* objective = reinterpret_cast<const SDK::FCrObjectiveData*>(row);
        if (SameGuid(objective->EntryID, id))
        {
            return objective;
        }
    }
    return nullptr;
}

std::string BuildObjectives(SDK::ACrGameStateBase* gameState, bool& truncated, bool& available)
{
    SDK::ACrObjectivesOwner* owner = gameState == nullptr ? nullptr : gameState->ObjectivesOwner;
    if (owner == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    const auto& entries = owner->ObjectivesEntriesContainer.EntriesStatusData;
    const int entryCount = entries.Num();
    const int safeEntryCount = std::clamp(entryCount, 0, 512);
    truncated = entryCount > safeEntryCount;
    int activeCount = 0;
    int completedCount = 0;
    int remainingSubObjectives = MaxSubObjectives;
    std::vector<std::string> active;
    for (int index = 0; index < safeEntryCount; ++index)
    {
        if (!entries.IsValidIndex(index))
        {
            continue;
        }
        const SDK::FCrObjectiveEntryStatus& status = entries[index];
        if (status.ObjectiveState == SDK::ECrObjectiveState::Completed)
        {
            ++completedCount;
        }
        if (status.ObjectiveState != SDK::ECrObjectiveState::Active)
        {
            continue;
        }
        ++activeCount;
        if (active.size() >= MaxObjectives)
        {
            truncated = true;
            continue;
        }
        Fields objectiveFields;
        Add(objectiveFields, "id", JsonString(GuidString(status.EntryID)));
        Add(objectiveFields, "state", JsonString(ObjectiveStateName(status.ObjectiveState)));
        const SDK::FCrObjectiveData* definition = FindObjectiveDefinition(status.EntryID);
        if (definition != nullptr)
        {
            const std::string title = SafeText(definition->EntryText);
            if (!title.empty())
            {
                Add(objectiveFields, "title", JsonString(title));
            }
        }
        std::vector<std::string> subObjectives;
        const int subCount = std::min(
            status.SubObjectives.Num(), remainingSubObjectives);
        if (status.SubObjectives.Num() > remainingSubObjectives)
        {
            truncated = true;
        }
        for (int subIndex = 0; subIndex < subCount; ++subIndex)
        {
            if (!status.SubObjectives.IsValidIndex(subIndex))
            {
                continue;
            }
            const SDK::FCrSubObjectiveEntryStatus& subStatus = status.SubObjectives[subIndex];
            Fields subFields;
            if (definition != nullptr && definition->SubObjectives.IsValidIndex(subIndex))
            {
                const std::string title = SafeText(definition->SubObjectives[subIndex].EntryText);
                if (!title.empty())
                {
                    Add(subFields, "title", JsonString(title));
                }
            }
            Add(subFields, "current", JsonInteger(subStatus.CurrentValue));
            Add(subFields, "required", JsonInteger(subStatus.ConditionValue));
            Add(subFields, "show_counter", JsonBoolean(subStatus.bShowCounter));
            subObjectives.push_back(JsonObject(subFields));
        }
        remainingSubObjectives -= subCount;
        Add(objectiveFields, "sub_objectives", JsonArray(subObjectives));
        active.push_back(JsonObject(objectiveFields));
    }
    Fields fields;
    Add(fields, "active_count", JsonInteger(activeCount));
    Add(fields, "completed_count", JsonInteger(completedCount));
    Add(fields, "active", JsonArray(active));
    Add(fields, "truncated", JsonBoolean(truncated));
    return JsonObject(fields);
}

std::string BuildEnvironment(SDK::UWorld* world, SDK::ACrGameStateBase* gameState, bool& available)
{
    auto* subsystem = static_cast<SDK::UCrEnviroWaveSubsystem*>(
        SDK::USubsystemBlueprintLibrary::GetWorldSubsystem(
            world, SDK::UCrEnviroWaveSubsystem::StaticClass()));
    if (subsystem == nullptr && gameState == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    Fields fields;
    if (subsystem != nullptr)
    {
        Add(fields, "wave_type", JsonString(WaveTypeName(subsystem->GetCurrentType())));
        Add(fields, "wave_stage", JsonString(WaveStageName(subsystem->GetCurrentStage())));
        Add(fields, "wave_in_progress", JsonBoolean(subsystem->IsWaveInProgress()));
        Add(fields, "wave_paused", JsonBoolean(subsystem->IsWavePaused()));
        AddNumber(fields, "stage_progress", subsystem->GetCurrentStageProgress());
        AddNumber(fields, "seconds_since_wave_started", subsystem->GetTimeSinceLastWaveStarted());
    }
    if (gameState != nullptr)
    {
        Add(fields, "sulfur_active", JsonBoolean(gameState->bSulphurActive));
        if (gameState->WaveTimerActor != nullptr)
        {
            AddNumber(fields, "next_wave_time", gameState->WaveTimerActor->NextTime);
            Add(fields, "next_wave_phase", JsonInteger(gameState->WaveTimerActor->NextPhase));
            Add(fields, "wave_timer_paused", JsonBoolean(gameState->WaveTimerActor->bPause));
        }
    }
    return JsonObject(fields);
}

std::string BuildTarget(
    SDK::UWorld* world,
    SDK::ACrCharacterPlayerBase* player,
    bool& available,
    bool& truncated)
{
    SDK::APlayerController* baseController = SDK::UGameplayStatics::GetPlayerController(world, 0);
    SDK::ACrPlayerControllerBase* controller =
        baseController != nullptr && baseController->IsA(SDK::ACrPlayerControllerBase::StaticClass())
        ? static_cast<SDK::ACrPlayerControllerBase*>(baseController)
        : nullptr;
    SDK::AActor* target = controller == nullptr
        ? nullptr
        : controller->CurrentInteractableActorWithActiveInteraction;
    if (target == nullptr && controller != nullptr)
    {
        target = controller->GetCurrentInteractable();
    }
    if (target == nullptr && controller != nullptr)
    {
        target = controller->CurrentBuildingTarget;
    }
    if (target == nullptr)
    {
        target = player->LookingBuilding;
    }
    if (target == nullptr)
    {
        target = player->CurrentAimingTargetActor;
    }
    if (target == nullptr)
    {
        available = true;
        return "null";
    }
    available = true;
    Fields fields;
    Add(fields, "name", JsonString(ObjectName(target)));
    Add(fields, "class", JsonString(ClassName(target)));
    const SDK::FVector playerLocation = player->K2_GetActorLocation();
    const SDK::FVector targetLocation = target->K2_GetActorLocation();
    AddNumber(fields, "distance_m", playerLocation.GetDistanceToInMeters(targetLocation));

    if (target->IsA(SDK::ACrBuildingActorBase::StaticClass()))
    {
        auto* building = static_cast<SDK::ACrBuildingActorBase*>(target);
        Fields buildingFields;
        if (building->PlacementData != nullptr
            && building->PlacementData->IsA(SDK::UCrBuildingData::StaticClass()))
        {
            const auto* buildingData = static_cast<SDK::UCrBuildingData*>(
                building->PlacementData);
            const std::string displayName = SafeText(buildingData->BuildingName);
            if (!displayName.empty())
            {
                Add(fields, "display_name", JsonString(displayName));
            }
        }
        Add(buildingFields, "state", JsonString(BuildingStateName(building->GetBuildingState())));
        Add(buildingFields, "turned_on", JsonBoolean(building->IsBuildingTurnedOn()));
        Add(buildingFields, "disabled", JsonBoolean(building->IsBuildingDisabled()));
        Add(buildingFields, "infected", JsonBoolean(building->IsBuildingInfectionActive()));
        AddNumber(buildingFields, "infection", building->GetCurrentInfection());
        AddNumber(buildingFields, "temperature", building->GetCurrentTemperature());
        Add(buildingFields, "grid_connection", JsonString(
                GridConnectionName(building->GetGridConnectionStatus())));
        AddNumber(buildingFields, "building_power", building->GetBuildingPower());
        AddNumber(buildingFields, "building_potential_power", building->GetBuildingPotentialPower());
        AddNumber(buildingFields, "grid_power", building->GetGridPower());
        AddNumber(buildingFields, "grid_add_power", building->GetGridAddPower());
        AddNumber(buildingFields, "grid_remove_power", building->GetGridRemovePower());
        if (target->IsA(SDK::ACrCrafter::StaticClass()))
        {
            auto* crafter = static_cast<SDK::ACrCrafter*>(target);
            AddNumber(buildingFields, "craft_progress", crafter->GetItemCraftingProgress());
            if (crafter->CraftComponent != nullptr)
            {
                Add(buildingFields, "craft_queue_size", JsonInteger(
                        std::max(0, crafter->CraftComponent->ItemsToCraft.Num())));
                if (crafter->CraftComponent->ItemsToCraft.IsValidIndex(0))
                {
                    const SDK::FAuCraftItem& queued = crafter->CraftComponent->ItemsToCraft[0];
                    const std::string outputName = ItemName(queued.OutputItem.ItemDataBase);
                    if (!outputName.empty())
                    {
                        Add(buildingFields, "crafting_item", JsonString(outputName));
                        Add(buildingFields, "crafting_amount", JsonInteger(queued.OutputItem.Count));
                    }
                }
            }
        }
        Add(fields, "building", JsonObject(buildingFields));
    }

    if (controller != nullptr
        && controller->CurrentInteractableActorWithActiveInteraction == target)
    {
        std::vector<std::string> interactedItems;
        const int count = std::min(
            controller->ItemListForInteractedBuilding.Num(), MaxInteractedItems);
        truncated = controller->ItemListForInteractedBuilding.Num() > MaxInteractedItems;
        for (int index = 0; index < count; ++index)
        {
            if (!controller->ItemListForInteractedBuilding.IsValidIndex(index))
            {
                continue;
            }
            const std::string name = ItemName(controller->ItemListForInteractedBuilding[index]);
            if (!name.empty())
            {
                interactedItems.push_back(JsonString(name));
            }
        }
        if (!interactedItems.empty())
        {
            Add(fields, "interacted_item_types", JsonArray(interactedItems));
        }
    }
    return JsonObject(fields);
}

std::string BuildBase(SDK::ACrGameStateBase* gameState, bool& available)
{
    SDK::ACrBaseCoreReplicationHelper* helper = gameState == nullptr
        ? nullptr
        : gameState->BaseCoreReplicationHelper;
    SDK::ACrMapMenuDataReplicationHelper* map = gameState == nullptr
        ? nullptr
        : gameState->MapMenuDataReplicationHelper;
    if (helper == nullptr && map == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    Fields fields;
    if (helper != nullptr)
    {
        Add(fields, "alien_activity", JsonString(AlienActivityName(helper->HUDAlienActivityType)));
        AddNumber(fields, "core_integrity", helper->HUDAlienActivityCoreIntegrity);
        AddNumber(fields, "alien_signature", helper->HUDAlienActivityAlienSignature);
    }
    if (map != nullptr)
    {
        Add(fields, "map_radiation_level", JsonInteger(map->CurrentRadiationLevelReplicated));
        Add(fields, "active_base_attack_markers", JsonInteger(
                std::max(0, map->BaseAttackMarkerDataContainer.BaseAttackMarkerData.Num())));
        Add(fields, "known_points_of_interest", JsonInteger(
                std::max(0, map->PointOfInterestReplicationData.Num())));
    }
    return JsonObject(fields);
}

std::string BuildUnavailableSnapshot(const std::string_view reason)
{
    std::vector<std::string> missing = {
        JsonString("player"), JsonString("inventory"), JsonString("gems"),
        JsonString("equipment"),
        JsonString("session"), JsonString("progression"), JsonString("objectives"),
        JsonString("environment"), JsonString("target"), JsonString("base")};
    Fields status;
    Add(status, "available", "false");
    Add(status, "partial", "true");
    Add(status, "reason", JsonString(reason));
    Add(status, "missing_sections", JsonArray(missing));
    Add(status, "truncated_sections", "[]");
    Fields root;
    Add(root, "schema_version", "1");
    Add(root, "captured_at_unix_ms", JsonInteger(UnixTimeMilliseconds()));
    Fields source;
    Add(source, "kind", JsonString("client_observed"));
    Add(source, "game_sdk_build", JsonString("CL121391"));
    Add(source, "sample_interval_ms", JsonInteger(750));
    Add(root, "source", JsonObject(source));
    Add(root, "status", JsonObject(status));
    return JsonObject(root);
}

std::string Capture(SDK::UWorld* world, bool& technologyTextConverted)
{
    if (world == nullptr)
    {
        return BuildUnavailableSnapshot("world_unavailable");
    }
    SDK::APawn* pawn = SDK::UGameplayStatics::GetPlayerPawn(world, 0);
    if (pawn == nullptr || !pawn->IsA(SDK::ACrCharacterPlayerBase::StaticClass()))
    {
        return BuildUnavailableSnapshot("local_player_unavailable");
    }
    auto* player = static_cast<SDK::ACrCharacterPlayerBase*>(pawn);
    if (!player->bCharacterSelectedAndInitialized)
    {
        return BuildUnavailableSnapshot("local_player_initializing");
    }
    SDK::AGameStateBase* baseGameState = SDK::UGameplayStatics::GetGameState(world);
    SDK::ACrGameStateBase* gameState =
        baseGameState != nullptr && baseGameState->IsA(SDK::ACrGameStateBase::StaticClass())
        ? static_cast<SDK::ACrGameStateBase*>(baseGameState)
        : nullptr;
    SDK::APlayerController* baseController = SDK::UGameplayStatics::GetPlayerController(world, 0);
    SDK::ACrPlayerControllerBase* controller =
        baseController != nullptr && baseController->IsA(SDK::ACrPlayerControllerBase::StaticClass())
        ? static_cast<SDK::ACrPlayerControllerBase*>(baseController)
        : nullptr;

    std::vector<std::string> missing;
    std::vector<std::string> truncatedSections;
    bool inventoryAvailable = false;
    bool inventoryTruncated = false;
    const std::string inventory = BuildInventory(
        player, inventoryTruncated, inventoryAvailable);
    if (!inventoryAvailable)
    {
        AddSectionName(missing, "inventory");
    }
    if (inventoryTruncated)
    {
        AddSectionName(truncatedSections, "inventory");
    }

    bool gemsAvailable = false;
    bool gemsTruncated = false;
    const std::string gems = BuildGems(player, gemsTruncated, gemsAvailable);
    if (!gemsAvailable)
    {
        AddSectionName(missing, "gems");
    }
    if (gemsTruncated)
    {
        AddSectionName(truncatedSections, "gems");
    }

    bool objectivesAvailable = false;
    bool objectivesTruncated = false;
    const std::string objectives = BuildObjectives(
        gameState, objectivesTruncated, objectivesAvailable);
    if (!objectivesAvailable)
    {
        AddSectionName(missing, "objectives");
    }
    if (objectivesTruncated)
    {
        AddSectionName(truncatedSections, "objectives");
    }

    bool environmentAvailable = false;
    const std::string environment = BuildEnvironment(world, gameState, environmentAvailable);
    if (!environmentAvailable)
    {
        AddSectionName(missing, "environment");
    }

    bool targetAvailable = false;
    bool targetTruncated = false;
    const std::string target = BuildTarget(world, player, targetAvailable, targetTruncated);
    if (!targetAvailable)
    {
        AddSectionName(missing, "target");
    }
    if (targetTruncated)
    {
        AddSectionName(truncatedSections, "target");
    }

    bool baseAvailable = false;
    const std::string base = BuildBase(gameState, baseAvailable);
    if (!baseAvailable)
    {
        AddSectionName(missing, "base");
    }

    bool sessionAvailable = false;
    bool sessionTruncated = false;
    const std::string session = BuildSession(
        gameState, player, sessionAvailable, sessionTruncated);
    if (!sessionAvailable)
    {
        AddSectionName(missing, "session");
    }
    if (sessionTruncated)
    {
        AddSectionName(truncatedSections, "session");
    }

    bool progressionAvailable = false;
    bool progressionTruncated = false;
    const std::string progression = BuildProgression(
        gameState, controller, progressionAvailable, progressionTruncated,
        technologyTextConverted);
    if (!progressionAvailable)
    {
        AddSectionName(missing, "progression");
    }
    if (progressionTruncated)
    {
        AddSectionName(truncatedSections, "progression");
    }

    Fields statusFields;
    if (player->CrPS != nullptr)
    {
        Add(statusFields, "dead", JsonBoolean(player->CrPS->bDead));
    }
    Add(statusFields, "incapacitated", JsonBoolean(player->IsIncapacitated()));
    Add(statusFields, "sprinting", JsonBoolean(player->IsSprinting()));
    Add(statusFields, "afk", JsonBoolean(player->IsAFK()));
    Add(statusFields, "interior", JsonBoolean(player->IsInInterior()));
    Add(statusFields, "safe_interior", JsonBoolean(player->IsInSafeInterior()));
    Add(statusFields, "protected", JsonBoolean(player->IsProtected()));
    Add(statusFields, "in_combat", JsonBoolean(player->bIsInCombatState));
    Add(statusFields, "profession", JsonString(ProfessionName(player->Profession)));

    const SDK::FVector location = player->K2_GetActorLocation();
    const SDK::FRotator rotation = player->K2_GetActorRotation();
    Fields positionFields;
    AddNumber(positionFields, "x_m", location.X / 100.0);
    AddNumber(positionFields, "y_m", location.Y / 100.0);
    AddNumber(positionFields, "z_m", location.Z / 100.0);
    AddNumber(positionFields, "yaw_degrees", rotation.Yaw);

    Fields corePlayerFields;
    Add(corePlayerFields, "status", JsonObject(statusFields));
    Add(corePlayerFields, "position", JsonObject(positionFields));
    Add(corePlayerFields, "vitals", BuildVitals(player));
    Add(corePlayerFields, "equipment", BuildEquipment(player));
    Fields playerFields = corePlayerFields;
    Add(playerFields, "inventory", inventory);
    Add(playerFields, "gems", gems);

    Fields snapshotStatus;
    Add(snapshotStatus, "available", "true");
    Add(snapshotStatus, "partial", JsonBoolean(
            !missing.empty() || !truncatedSections.empty()));
    Add(snapshotStatus, "missing_sections", JsonArray(missing));
    Add(snapshotStatus, "truncated_sections", JsonArray(truncatedSections));

    Fields root;
    Add(root, "schema_version", "1");
    Add(root, "captured_at_unix_ms", JsonInteger(UnixTimeMilliseconds()));
    Fields sourceFields;
    Add(sourceFields, "kind", JsonString("client_observed"));
    Add(sourceFields, "game_sdk_build", JsonString("CL121391"));
    Add(sourceFields, "sample_interval_ms", JsonInteger(750));
    Add(root, "source", JsonObject(sourceFields));
    Add(root, "status", JsonObject(snapshotStatus));
    Add(root, "session", session);
    Add(root, "player", JsonObject(playerFields));
    Add(root, "progression", progression);
    Add(root, "objectives", objectives);
    Add(root, "environment", environment);
    Add(root, "target", target);
    Add(root, "base", base);
    std::string snapshot = JsonObject(root);
    if (snapshot.size() > MaxSnapshotBytes)
    {
        AddSectionName(truncatedSections, "inventory");
        AddSectionName(truncatedSections, "gems");
        AddSectionName(truncatedSections, "progression");
        AddSectionName(truncatedSections, "objectives");
        Fields prunedStatus;
        Add(prunedStatus, "available", "true");
        Add(prunedStatus, "partial", "true");
        Add(prunedStatus, "reason", JsonString("snapshot_pruned"));
        Add(prunedStatus, "missing_sections", JsonArray(missing));
        Add(prunedStatus, "truncated_sections", JsonArray(truncatedSections));
        Fields prunedPlayer = corePlayerFields;
        Add(prunedPlayer, "inventory", "null");
        Add(prunedPlayer, "gems", "null");
        Fields prunedRoot;
        Add(prunedRoot, "schema_version", "1");
        Add(prunedRoot, "captured_at_unix_ms", JsonInteger(UnixTimeMilliseconds()));
        Add(prunedRoot, "source", JsonObject(sourceFields));
        Add(prunedRoot, "status", JsonObject(prunedStatus));
        Add(prunedRoot, "session", session);
        Add(prunedRoot, "player", JsonObject(prunedPlayer));
        Add(prunedRoot, "progression", "null");
        Add(prunedRoot, "objectives", "null");
        Add(prunedRoot, "environment", environment);
        Add(prunedRoot, "target", target);
        Add(prunedRoot, "base", base);
        snapshot = JsonObject(prunedRoot);
        if (snapshot.size() > MaxSnapshotBytes)
        {
            Fields minimalRoot;
            Add(minimalRoot, "schema_version", "1");
            Add(minimalRoot, "captured_at_unix_ms", JsonInteger(UnixTimeMilliseconds()));
            Add(minimalRoot, "source", JsonObject(sourceFields));
            Add(minimalRoot, "status", JsonObject(prunedStatus));
            Add(minimalRoot, "player", JsonObject(corePlayerFields));
            snapshot = JsonObject(minimalRoot);
        }
    }
    return snapshot;
}

void StoreSnapshot(std::string snapshot)
{
    std::scoped_lock lock(g_snapshotMutex);
    g_snapshot = std::move(snapshot);
}

SDK::UWorld* RecoverCurrentWorld()
{
    SDK::UWorld* world = SDK::UWorld::GetWorld();
    SDK::APawn* pawn = world == nullptr
        ? nullptr
        : SDK::UGameplayStatics::GetPlayerPawn(world, 0);
    return pawn != nullptr && pawn->IsA(SDK::ACrCharacterPlayerBase::StaticClass())
        ? world
        : nullptr;
}

void RecordGameThread(void*)
{
    g_gameThreadId.store(GetCurrentThreadId(), std::memory_order_release);
}

void OnWorldBeginPlay(SDK::UWorld* world)
{
    RecordGameThread(nullptr);
    g_world = world;
    g_loggedSuccessfulSample = false;
    g_nextSampleAt = SteadyClock::now();
    StoreSnapshot(BuildUnavailableSnapshot("waiting_for_local_player"));
}

void OnWorldEndPlay(SDK::UWorld* world, const char*)
{
    RecordGameThread(nullptr);
    if (world == g_world)
    {
        g_world = nullptr;
        g_nextWorldProbeAt = SteadyClock::now() + WorldProbeInterval;
        StoreSnapshot(BuildUnavailableSnapshot("world_ended"));
    }
}

void OnEngineTick(const float)
{
    g_gameThreadId.store(GetCurrentThreadId(), std::memory_order_release);
    const SteadyClock::time_point now = SteadyClock::now();
    if (g_world == nullptr)
    {
        if (now < g_nextWorldProbeAt)
        {
            return;
        }
        g_nextWorldProbeAt = now + WorldProbeInterval;
        g_world = RecoverCurrentWorld();
        if (g_world == nullptr)
        {
            return;
        }
        g_nextSampleAt = now;
    }
    if (now < g_nextSampleAt)
    {
        return;
    }
    g_nextSampleAt = now + SampleInterval;
    try
    {
        bool technologyTextConverted = false;
        std::string snapshot = Capture(g_world, technologyTextConverted);
        StoreSnapshot(std::move(snapshot));
        if (technologyTextConverted && !g_loggedSuccessfulSample
            && g_self != nullptr && g_self->logger != nullptr)
        {
            g_loggedSuccessfulSample = true;
            g_self->logger->Info(
                g_self, "%s", "Live game-state sample captured");
        }
    }
    catch (...)
    {
        StoreSnapshot(BuildUnavailableSnapshot("capture_failed"));
        if (g_self != nullptr && g_self->logger != nullptr)
        {
            g_self->logger->Error(g_self, "%s", "Live game-state capture failed");
        }
    }
}

void UnregisterOnGameThread(void*)
{
    if (g_registered && g_self != nullptr && g_self->hooks != nullptr)
    {
        if (g_self->hooks->Engine != nullptr)
        {
            g_self->hooks->Engine->UnregisterOnTick(&OnEngineTick);
        }
        if (g_self->hooks->World != nullptr)
        {
            g_self->hooks->World->UnregisterOnWorldBeginPlay(&OnWorldBeginPlay);
            g_self->hooks->World->UnregisterOnBeforeWorldEndPlay(&OnWorldEndPlay);
        }
        g_registered = false;
        g_world = nullptr;
    }
}
} // namespace

bool Initialize(IPluginSelf* self)
{
    if (g_registered)
    {
        return true;
    }
    g_self = self;
    StoreSnapshot(BuildUnavailableSnapshot("world_unavailable"));
    if (!CompatibleGameBuild())
    {
        StoreSnapshot(BuildUnavailableSnapshot("unsupported_game_build"));
        return false;
    }
    if (self == nullptr || self->hooks == nullptr || self->hooks->Engine == nullptr
        || self->hooks->Engine->PostToGameThread == nullptr
        || self->hooks->World == nullptr || self->hooks->Memory == nullptr
        || self->hooks->Memory->Free == nullptr
        || self->hooks->Memory->IsAllocatorAvailable == nullptr
        || !self->hooks->Memory->IsAllocatorAvailable())
    {
        return false;
    }
    g_freeEngineMemory = self->hooks->Memory->Free;
    g_loggedSuccessfulSample = false;
    g_cleanupEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (g_cleanupEvent == nullptr)
    {
        g_freeEngineMemory = nullptr;
        return false;
    }
    g_registrationThreadId.store(
        GetCurrentThreadId(), std::memory_order_release);
    g_registeredDuringStartup.store(
        self->hooks->Splash != nullptr
        && self->hooks->Splash->IsVisible != nullptr
        && self->hooks->Splash->IsVisible(),
        std::memory_order_release);
    // Mark the transaction active before the first registration so an
    // exception from a later host-side vector allocation rolls back every hook.
    g_registered = true;
    self->hooks->Engine->RegisterOnTick(&OnEngineTick);
    self->hooks->World->RegisterOnWorldBeginPlay(&OnWorldBeginPlay);
    self->hooks->World->RegisterOnBeforeWorldEndPlay(&OnWorldEndPlay);
    g_nextWorldProbeAt = SteadyClock::now();
    return true;
}

void Shutdown()
{
    if (g_registered && g_self != nullptr && g_self->hooks != nullptr
        && g_self->hooks->Engine != nullptr)
    {
        const DWORD currentThreadId = GetCurrentThreadId();
        const DWORD gameThreadId = g_gameThreadId.load(std::memory_order_acquire);
        const bool startupRegistrationThread =
            g_registeredDuringStartup.load(std::memory_order_acquire)
            && g_registrationThreadId.load(std::memory_order_acquire)
                == currentThreadId;
        if ((gameThreadId != 0 && gameThreadId == currentThreadId)
            || CurrentThreadOwnsProcessWindow() || startupRegistrationThread)
        {
            UnregisterOnGameThread(nullptr);
        }
        else
        {
            ResetEvent(g_cleanupEvent);
            g_self->hooks->Engine->PostToGameThread(&UnregisterOnGameThread, nullptr);
            // This callback lives in KernelBase, so the game thread has fully
            // returned from plugin cleanup before the waiting thread may unload
            // this DLL. StarRupture is x64-only; SetEvent's single HANDLE
            // argument uses the same Win64 call ABI and its BOOL is ignored.
            static_assert(sizeof(&SetEvent) == sizeof(PluginGameThreadCallback));
            const auto signalEvent = std::bit_cast<PluginGameThreadCallback>(&SetEvent);
            g_self->hooks->Engine->PostToGameThread(
                signalEvent, g_cleanupEvent);
            WaitForSingleObject(g_cleanupEvent, INFINITE);
        }
    }
    g_registered = false;
    g_world = nullptr;
    g_gameThreadId.store(0, std::memory_order_release);
    g_registrationThreadId.store(0, std::memory_order_release);
    g_registeredDuringStartup.store(false, std::memory_order_release);
    g_freeEngineMemory = nullptr;
    g_loggedSuccessfulSample = false;
    if (g_cleanupEvent != nullptr)
    {
        CloseHandle(g_cleanupEvent);
        g_cleanupEvent = nullptr;
    }
    g_self = nullptr;
    StoreSnapshot(BuildUnavailableSnapshot("plugin_stopped"));
}

std::string Snapshot()
{
    std::scoped_lock lock(g_snapshotMutex);
    return g_snapshot;
}
} // namespace RuptureCompanion::LiveContext
